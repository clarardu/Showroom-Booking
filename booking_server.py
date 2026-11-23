"""
Appointment booking server for an AssemblyAI Voice Agent.

The voice agent calls these two endpoints as HTTP tools:
  POST /tools/check_availability   {"date": "2026-11-24", "time": "14:00"}
  POST /tools/book_appointment     {"start": "2026-11-24T10:00", "name": "Jane Doe",
                                    "phone": "+14035550123", "piano_type": "grand",
                                    "budget": "$20,000-30,000", "brands": ["Yamaha", "Kawai"],
                                    "notes": "Wants to try a baby grand"}

It reads and writes Google Calendar directly (fast enough for a live call), then
optionally pings OpenClaw so it can do the slower follow-up work (SMS confirmation,
notify you on Telegram/Slack, reminders).

Setup:
  pip install fastapi uvicorn google-api-python-client google-auth requests
  export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service-account.json)"   # or GOOGLE_SERVICE_ACCOUNT_FILE=path
  export CALENDAR_ID=you@yourbusiness.com                    # or the calendar's ID
  export TOOL_SECRET=some-long-random-string                 # put ?key=... on the tool URLs
  export OPENCLAW_HOOK_URL=...                               # optional, see guide
  uvicorn booking_server:app --host 0.0.0.0 --port 8000
"""

import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException, Request
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Check settings up front so a missing one gives a clear message, not a crash.
_missing = [v for v in ("CALENDAR_ID", "TOOL_SECRET") if not os.getenv(v, "").strip()]
if not (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip() or os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()):
    _missing.append("GOOGLE_SERVICE_ACCOUNT_JSON")
if _missing:
    raise SystemExit(
        "\n*** Missing settings: " + ", ".join(_missing) + " ***\n"
        "Add them in Railway: click your service > Variables tab > New Variable, "
        "then click Deploy to apply.\n"
    )

TZ = ZoneInfo(os.getenv("BUSINESS_TZ", "America/Edmonton").strip())
CALENDAR_ID = os.environ["CALENDAR_ID"].strip()
TOOL_SECRET = os.environ["TOOL_SECRET"].strip()
OPENCLAW_HOOK_URL = os.getenv("OPENCLAW_HOOK_URL")
OPENCLAW_HOOK_TOKEN = os.getenv("OPENCLAW_HOOK_TOKEN")

# ---------------------------------------------------------------------------
# GUARDRAILS - edit these to match your business. The server enforces them on
# every request, so the voice agent can't book outside them even if it tries.
# ---------------------------------------------------------------------------
OPEN_HOUR, CLOSE_HOUR = 10, 17         # 10am-5pm; appointments must END by 5pm
WORK_DAYS = {1, 2, 3, 4, 5, 6}         # 0=Mon ... 6=Sun -> closed Monday, open Tue-Sun
SLOT_STEP = timedelta(minutes=30)      # offer start times every 30 min
MIN_NOTICE = timedelta(hours=2)        # no booking someone 10 minutes from now
MAX_DAYS_AHEAD = 60                    # no booking a year out
ALLOWED_DURATIONS = {30}               # every appointment is 30 minutes
PIANO_TYPES = {"upright", "grand", "both", "not sure"}
CLOSED_DATES: set[str] = set()         # holidays, e.g. {"2026-12-25", "2027-01-01"}
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_days = sorted(WORK_DAYS)
_days_txt = (f"{DAY_NAMES[_days[0]]} to {DAY_NAMES[_days[-1]]}"
             if len(_days) > 2 and _days == list(range(_days[0], _days[-1] + 1))
             else ", ".join(DAY_NAMES[d] for d in _days))
HOURS_SPOKEN = (
    f"We're open {_days_txt}"
    + f", {OPEN_HOUR % 12 or 12}{'am' if OPEN_HOUR < 12 else 'pm'} to "
    + f"{CLOSE_HOUR % 12 or 12}{'am' if CLOSE_HOUR < 12 else 'pm'}."
)


def is_open_day(day) -> bool:
    return day.weekday() in WORK_DAYS and str(day) not in CLOSED_DATES


def clean_duration(args) -> timedelta:
    try:
        minutes = int(args.get("duration_minutes", 30))
    except (TypeError, ValueError):
        minutes = 30
    return timedelta(minutes=minutes if minutes in ALLOWED_DURATIONS else 30)


def rule_violation(start: datetime, length: timedelta) -> str | None:
    """Return a caller-friendly reason if this slot breaks a business rule, else None."""
    now = datetime.now(TZ)
    day = start.date()
    if start < now + MIN_NOTICE:
        return "That's too soon. We need at least a couple of hours' notice."
    if day > (now + timedelta(days=MAX_DAYS_AHEAD)).date():
        return f"We only book up to {MAX_DAYS_AHEAD} days ahead."
    if str(day) in CLOSED_DATES:
        return "We're closed that day for a holiday."
    if day.weekday() not in WORK_DAYS:
        return f"We're closed on {DAY_NAMES[day.weekday()]}s. {HOURS_SPOKEN}"
    open_t = start.replace(hour=OPEN_HOUR, minute=0)
    close_t = start.replace(hour=CLOSE_HOUR, minute=0)
    if start < open_t or start + length > close_t:
        return f"That's outside our hours. {HOURS_SPOKEN}"
    return None

_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _load_key_json(text: str) -> dict:
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        raise SystemExit(
            "\n*** GOOGLE_SERVICE_ACCOUNT_JSON isn't valid. Open the downloaded key file in a "
            "text editor and paste ALL of it, from the first { to the last }. ***\n"
        )
if os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
    # Hosting-friendly: paste the whole key file's contents into this env variable.
    creds = service_account.Credentials.from_service_account_info(
        _load_key_json(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]), scopes=_SCOPES)
else:
    creds = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"], scopes=_SCOPES)
cal = build("calendar", "v3", credentials=creds, cache_discovery=False)

app = FastAPI()


@app.get("/")
def health():
    return {"ok": True, "hours": HOURS_SPOKEN}


def check_key(request: Request):
    if request.query_params.get("key") != TOOL_SECRET:
        raise HTTPException(status_code=401, detail="bad key")


async def tool_args(request: Request) -> dict:
    """Accept either the raw arguments or an {"arguments": {...}} wrapper."""
    body = await request.json()
    # Shows up in Railway's logs so you can see exactly what the agent sent.
    print(f"[tool] {request.url.path} <- {json.dumps(body)[:500]}", flush=True)
    return body.get("arguments", body) if isinstance(body, dict) else {}


def busy_blocks(start: datetime, end: datetime):
    fb = cal.freebusy().query(body={
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "timeZone": str(TZ),
        "items": [{"id": CALENDAR_ID}],
    }).execute()
    return [
        (datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
        for b in fb["calendars"][CALENDAR_ID]["busy"]
    ]


def is_free(start: datetime, end: datetime, busy) -> bool:
    return all(end <= b_start or start >= b_end for b_start, b_end in busy)


def free_slots_on(day, length):
    """All bookable start times on one day: open day, inside hours, enough notice, not busy."""
    if not is_open_day(day):
        return []
    day_start = datetime(day.year, day.month, day.day, OPEN_HOUR, tzinfo=TZ)
    day_end = datetime(day.year, day.month, day.day, CLOSE_HOUR, tzinfo=TZ)
    busy = busy_blocks(day_start, day_end)
    slots, t = [], day_start
    while t + length <= day_end:
        if rule_violation(t, length) is None and is_free(t, t + length, busy):
            slots.append(t)
        t += SLOT_STEP
    return slots


def closest_to(slots, preferred: datetime | None, n=3):
    """Pick the n slots nearest the caller's preferred time (or the earliest n)."""
    if preferred:
        slots = sorted(slots, key=lambda s: abs((s - preferred).total_seconds()))
    return sorted(slots[:n])


def spoken(s: datetime) -> str:
    return f"{s:%A %B} {s.day} at {s.hour % 12 or 12}:{s:%M} {'AM' if s.hour < 12 else 'PM'}"


def fmt(s: datetime) -> dict:
    # "start" is what the agent passes back to book_appointment; "spoken" is what it says.
    return {"start": s.strftime("%Y-%m-%dT%H:%M"), "spoken": spoken(s)}


def find_options(day, length, preferred=None, search_days=14):
    """
    The feedback loop: tell the agent whether the requested time works and, if not,
    say why (closed, outside hours, taken...) and hand it concrete alternatives.
    """
    today = datetime.now(TZ).date()
    if day < today:
        day, preferred = today, None  # caller (or agent) picked a past date: start from today

    same_day = free_slots_on(day, length)

    if preferred:
        problem = rule_violation(preferred, length)
        if problem is None and preferred in same_day:
            return {"requested_time_available": True, "requested": fmt(preferred)}
        if same_day:
            return {
                "requested_time_available": False,
                "reason": problem or "That exact time is taken.",
                "alternatives_same_day": [fmt(s) for s in closest_to(same_day, preferred)],
            }
    elif same_day:
        return {"requested_time_available": True,
                "available_slots": [fmt(s) for s in closest_to(same_day, None)]}

    # Whole day is closed, full, or past the booking window: look ahead.
    if str(day) in CLOSED_DATES:
        reason = "We're closed that day for a holiday."
    elif day.weekday() not in WORK_DAYS:
        reason = f"We're closed on {DAY_NAMES[day.weekday()]}s. {HOURS_SPOKEN}"
    elif preferred and rule_violation(preferred, length):
        reason = rule_violation(preferred, length)
    else:
        reason = "That day is fully booked."

    next_days = []
    for i in range(1, search_days + 1):
        d = day + timedelta(days=i)
        slots = free_slots_on(d, length)
        if slots:
            target = preferred.replace(year=d.year, month=d.month, day=d.day) if preferred else None
            next_days.append(fmt(closest_to(slots, target, n=1)[0]))
        if len(next_days) == 3:
            break
    return {
        "requested_time_available": False,
        "reason": reason,
        "next_available": next_days,
        **({} if next_days else {"note": f"Nothing open in the next {search_days} days. Offer to take a message."}),
    }


@app.post("/tools/get_today")
async def get_today(request: Request):
    """The agent calls this first so it can turn "next Tuesday" into a real date."""
    check_key(request)
    now = datetime.now(TZ)
    upcoming = [
        {"date": str((now + timedelta(days=i)).date()),
         "weekday": DAY_NAMES[(now + timedelta(days=i)).weekday()],
         "open": is_open_day((now + timedelta(days=i)).date())}
        for i in range(14)
    ]
    return {"today": str(now.date()), "weekday": DAY_NAMES[now.weekday()],
            "time_now": now.strftime("%H:%M"), "hours": HOURS_SPOKEN, "next_14_days": upcoming}


@app.post("/tools/check_availability")
async def check_availability(request: Request):
    """
    Args: date (YYYY-MM-DD), optional time (HH:MM, caller's preference), duration_minutes.
    """
    check_key(request)
    args = await tool_args(request)
    try:
        day = datetime.fromisoformat(str(args["date"])).date()
        preferred = None
        if args.get("time"):
            h, m = map(int, str(args["time"]).split(":")[:2])
            preferred = datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)
    except (KeyError, ValueError):
        return {"error": "I couldn't understand that date or time. Ask the caller to repeat it.",
                "hours": HOURS_SPOKEN}
    result = find_options(day, clean_duration(args), preferred)
    result["hours"] = HOURS_SPOKEN
    return result


@app.post("/tools/book_appointment")
async def book_appointment(request: Request):
    check_key(request)
    args = await tool_args(request)

    # Guardrail: required caller details.
    name = str(args.get("name", "")).strip()
    phone = str(args.get("phone", "")).strip()
    missing = [f for f, v in (("name", name), ("phone", phone)) if not v]
    if missing:
        return {"booked": False, "reason": f"Missing {' and '.join(missing)}. Ask the caller before booking."}

    try:
        start = datetime.fromisoformat(str(args["start"])).replace(tzinfo=TZ, second=0, microsecond=0)
    except (KeyError, ValueError):
        return {"booked": False, "reason": "Invalid start time. Call check_availability first."}
    length = clean_duration(args)
    end = start + length

    # Guardrail: business rules are checked here too, not just in check_availability.
    problem = rule_violation(start, length)
    if problem or (start - start.replace(hour=0, minute=0)) % SLOT_STEP:
        options = find_options(start.date(), length, start)
        return {"booked": False, "reason": problem or "Appointments start on the hour or half hour.",
                **{k: v for k, v in options.items() if k != "reason"}}

    # Re-check right before writing so two callers can't grab the same slot.
    if not is_free(start, end, busy_blocks(start, end)):
        options = find_options(start.date(), length, start)
        return {"booked": False, "reason": "That time was just taken.",
                **{k: v for k, v in options.items() if k != "reason"}}

    # Showroom visit details collected by the agent.
    piano_type = str(args.get("piano_type", "")).strip().lower()
    if piano_type not in PIANO_TYPES:
        piano_type = "not sure"
    budget = str(args.get("budget") or "not given").strip()[:80]
    brands = args.get("brands") or []
    if isinstance(brands, str):
        brands = [b.strip() for b in brands.split(",") if b.strip()]
    brands_txt = ", ".join(str(b)[:40] for b in brands[:6]) or "no preference"
    notes = str(args.get("notes") or "").strip()[:300]
    reason = f"{piano_type.title()} piano viewing" if piano_type != "not sure" else "Piano viewing"

    event = cal.events().insert(calendarId=CALENDAR_ID, body={
        "summary": f"{reason} - {name}"[:150],
        "description": (
            "Booked by phone agent.\n"
            f"Phone: {phone}\n"
            f"Piano type: {piano_type}\n"
            f"Budget: {budget}\n"
            f"Brands: {brands_txt}"
            + (f"\nNotes: {notes}" if notes else "")
        ),
        "start": {"dateTime": start.isoformat(), "timeZone": str(TZ)},
        "end": {"dateTime": end.isoformat(), "timeZone": str(TZ)},
    }).execute()

    if OPENCLAW_HOOK_URL:
        # Fire-and-forget hand-off: OpenClaw sends the SMS confirmation, pings you, etc.
        try:
            requests.post(
                OPENCLAW_HOOK_URL,
                headers={"Authorization": f"Bearer {OPENCLAW_HOOK_TOKEN}"} if OPENCLAW_HOOK_TOKEN else {},
                json={"message": (
                    f"New phone booking: {name} ({phone}), {reason} on {spoken(start)}. "
                    f"Budget: {budget}. Brands: {brands_txt}. "
                    f"Calendar event: {event.get('htmlLink')}. "
                    "Text the caller a confirmation and let me know."
                )},
                timeout=3,
            )
        except requests.RequestException:
            pass  # booking already succeeded; don't fail the call over a notification

    return {"booked": True, "start": spoken(start), "event_id": event["id"]}
