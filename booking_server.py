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


# ===========================================================================
# PHONE BRIDGE (works on a free Twilio trial - no SIP trunk needed)
#
# Twilio answers the call and streams its audio here over a WebSocket; this
# server passes it to your AssemblyAI agent and plays the agent's voice back.
# The agent still books through the HTTP tools above, exactly as before.
#
# Extra Railway variables needed for this part:
#   ASSEMBLYAI_API_KEY   your AssemblyAI API key
#   AGENT_ID             the "id" you got when you created the agent
# Twilio setup: on your number, set "A call comes in" to Webhook, HTTP POST,
#   https://YOUR-SERVER-URL/twilio/voice?key=YOUR_TOOL_SECRET
# ===========================================================================

import asyncio
import base64
from xml.sax.saxutils import escape as _xml_escape

import numpy as np
import websockets
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import Response

AAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY", "").strip()
AGENT_ID = os.getenv("AGENT_ID", "").strip()
# Agents made on agents.us.assemblyai.com live in the US region.
AAI_WS_URL = os.getenv("AAI_WS_URL", "wss://agents.us.assemblyai.com/v1/ws").strip()

# --- audio conversion: Twilio sends 8 kHz mu-law, the agent uses 24 kHz PCM16 ---
_BIAS, _CLIP = 0x84, 32635


def _build_ulaw_table():
    table = np.zeros(256, dtype=np.int16)
    for i in range(256):
        u = ~i & 0xFF
        sign, exponent, mantissa = u & 0x80, (u >> 4) & 0x07, u & 0x0F
        sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
        table[i] = -sample if sign else sample
    return table


_ULAW_TO_PCM = _build_ulaw_table()


def ulaw8k_to_pcm24k(data: bytes) -> bytes:
    pcm = _ULAW_TO_PCM[np.frombuffer(data, dtype=np.uint8)].astype(np.float32)
    if pcm.size == 0:
        return b""
    # 8k -> 24k: linear interpolation, 3 output samples per input sample
    x_old = np.arange(pcm.size)
    x_new = np.arange(pcm.size * 3) / 3.0
    up = np.interp(x_new, x_old, pcm)
    return up.astype(np.int16).tobytes()


def pcm24k_to_ulaw8k(data: bytes) -> bytes:
    pcm = np.frombuffer(data[: len(data) - len(data) % 2], dtype=np.int16).astype(np.int32)
    n = pcm.size - pcm.size % 3
    if n == 0:
        return b""
    # 24k -> 8k: average each group of 3 samples (simple low-pass + decimate)
    down = pcm[:n].reshape(-1, 3).mean(axis=1).astype(np.int32)
    sign = (down < 0)
    mag = np.minimum(np.abs(down), _CLIP) + _BIAS
    exponent = np.clip(np.floor(np.log2(mag)).astype(np.int32) - 7, 0, 7)
    mantissa = (mag >> (exponent + 3)) & 0x0F
    ulaw = ~((sign.astype(np.int32) << 7) | (exponent << 4) | mantissa) & 0xFF
    return ulaw.astype(np.uint8).tobytes()


@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    """Twilio calls this when someone rings your number; we tell it to stream the call here."""
    check_key(request)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    stream_url = f"wss://{host}/twilio/stream"
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
        f'<Stream url="{_xml_escape(stream_url)}">'
        f'<Parameter name="key" value="{_xml_escape(TOOL_SECRET)}"/>'
        "</Stream></Connect></Response>"
    )
    return Response(content=twiml, media_type="text/xml")


@app.websocket("/twilio/stream")
async def twilio_stream(twilio_ws: WebSocket):
    await twilio_ws.accept()
    stream_sid = None

    # Wait for Twilio's "start" message, which carries the stream id and our key.
    try:
        while stream_sid is None:
            msg = json.loads(await twilio_ws.receive_text())
            if msg.get("event") == "start":
                start = msg["start"]
                if start.get("customParameters", {}).get("key") != TOOL_SECRET:
                    print("[call] rejected: bad key", flush=True)
                    await twilio_ws.close()
                    return
                stream_sid = start["streamSid"]
    except (WebSocketDisconnect, KeyError, json.JSONDecodeError):
        return

    if not (AAI_API_KEY and AGENT_ID):
        print("[call] ASSEMBLYAI_API_KEY or AGENT_ID missing in Railway variables", flush=True)
        await twilio_ws.close()
        return

    print(f"[call] started {stream_sid}", flush=True)
    try:
        async with websockets.connect(
            AAI_WS_URL, additional_headers={"Authorization": f"Bearer {AAI_API_KEY}"}
        ) as aai_ws:
            await aai_ws.send(json.dumps({"type": "session.update", "session": {"agent_id": AGENT_ID}}))
            ready = asyncio.Event()

            async def caller_to_agent():
                while True:
                    msg = json.loads(await twilio_ws.receive_text())
                    event = msg.get("event")
                    if event == "media" and ready.is_set():
                        pcm = ulaw8k_to_pcm24k(base64.b64decode(msg["media"]["payload"]))
                        await aai_ws.send(json.dumps(
                            {"type": "input.audio", "audio": base64.b64encode(pcm).decode()}))
                    elif event == "stop":
                        return

            async def agent_to_caller():
                leftover = b""
                async for raw in aai_ws:
                    msg = json.loads(raw)
                    kind = msg.get("type")
                    if kind == "session.ready":
                        ready.set()
                        print("[call] agent ready", flush=True)
                    elif kind == "reply.audio":
                        pcm = leftover + base64.b64decode(msg.get("data") or msg.get("audio") or "")
                        cut = len(pcm) - len(pcm) % 6   # whole 3-sample groups only
                        pcm, leftover = pcm[:cut], pcm[cut:]
                        ulaw = pcm24k_to_ulaw8k(pcm)
                        if ulaw:
                            await twilio_ws.send_text(json.dumps({
                                "event": "media", "streamSid": stream_sid,
                                "media": {"payload": base64.b64encode(ulaw).decode()}}))
                    elif kind == "input.speech.started" or (
                            kind == "reply.done" and msg.get("status") == "interrupted"):
                        # Caller started talking: stop playing the agent's queued audio.
                        leftover = b""
                        await twilio_ws.send_text(json.dumps({"event": "clear", "streamSid": stream_sid}))
                    elif kind in ("transcript.user", "transcript.agent"):
                        who = "caller" if kind == "transcript.user" else "agent"
                        print(f"[call] {who}: {msg.get('text', '')}", flush=True)
                    elif kind in ("session.error", "error"):
                        print(f"[call] AssemblyAI error: {json.dumps(msg)[:500]}", flush=True)
                    elif kind == "session.ended":
                        return

            tasks = [asyncio.create_task(caller_to_agent()), asyncio.create_task(agent_to_caller())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                if t.exception() and not isinstance(t.exception(), (WebSocketDisconnect, websockets.ConnectionClosed)):
                    print(f"[call] bridge error: {t.exception()!r}", flush=True)
            try:
                await aai_ws.send(json.dumps({"type": "session.end"}))
            except websockets.ConnectionClosed:
                pass
    except websockets.InvalidStatus as e:
        print(f"[call] AssemblyAI refused the connection ({e.response.status_code}). "
              "Check ASSEMBLYAI_API_KEY, AGENT_ID and AAI_WS_URL.", flush=True)
    except Exception as e:  # keep the server alive whatever happens on one call
        print(f"[call] error: {e!r}", flush=True)
    finally:
        print(f"[call] ended {stream_sid}", flush=True)
        try:
            await twilio_ws.close()
        except Exception:
            pass


# ===========================================================================
# BROWSER TEST PAGE (free: no Twilio needed)
# Open https://YOUR-SERVER-URL/talk?key=YOUR_TOOL_SECRET in Chrome, click Start,
# and talk to your agent through your microphone. Same agent, same tools,
# real bookings in your calendar. Uses the ASSEMBLYAI_API_KEY and AGENT_ID variables.
# ===========================================================================

from fastapi.responses import HTMLResponse

_TALK_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Talk to your receptionist</title>
<style>
  body{font-family:-apple-system,system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 20px;color:#1d1d1f}
  h1{font-size:22px;margin-bottom:4px} p{color:#555;line-height:1.5}
  button{font-size:17px;padding:12px 28px;border-radius:24px;border:none;background:#1d1d1f;color:#fff;cursor:pointer}
  button.stop{background:#c0392b} #status{margin:16px 0;font-weight:600}
  #log{border-top:1px solid #ddd;margin-top:16px;padding-top:8px}
  .line{margin:8px 0;line-height:1.4} .you{color:#0a58ca} .agent{color:#1d1d1f} .sys{color:#888;font-size:14px}
</style></head><body>
<h1>Talk to your receptionist</h1>
<p>Click Start, allow the microphone, and talk as if you were a customer calling. Bookings it makes are real, so delete test events from your calendar afterwards. Headphones work best.</p>
<button id="btn">Start</button>
<div id="status">Not connected</div>
<div id="log"></div>
<script>
const KEY = new URLSearchParams(location.search).get("key") || "";
const btn = document.getElementById("btn"), statusEl = document.getElementById("status"), logEl = document.getElementById("log");
let ws, ctx, micStream, proc, src, playing = [], playHead = 0;

function log(cls, text){ const d=document.createElement("div"); d.className="line "+cls; d.textContent=text; logEl.prepend(d); }
function b64(buf){ let s="", b=new Uint8Array(buf); for(let i=0;i<b.length;i+=0x8000) s+=String.fromCharCode.apply(null,b.subarray(i,i+0x8000)); return btoa(s); }
function unb64(s){ const bin=atob(s), out=new Uint8Array(bin.length); for(let i=0;i<bin.length;i++) out[i]=bin.charCodeAt(i); return out.buffer; }

function stopPlayback(){ playing.forEach(n=>{try{n.stop()}catch(e){}}); playing=[]; playHead=ctx?ctx.currentTime:0; }
function play(pcmBuf){
  const i16=new Int16Array(pcmBuf), f32=new Float32Array(i16.length);
  for(let i=0;i<i16.length;i++) f32[i]=i16[i]/32768;
  const ab=ctx.createBuffer(1,f32.length,24000); ab.copyToChannel(f32,0);
  const n=ctx.createBufferSource(); n.buffer=ab; n.connect(ctx.destination);
  playHead=Math.max(playHead, ctx.currentTime+0.05); n.start(playHead); playHead+=ab.duration;
  playing.push(n); n.onended=()=>{playing=playing.filter(x=>x!==n)};
}

async function start(){
  btn.disabled=true; statusEl.textContent="Connecting...";
  try{
    micStream = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,channelCount:1}});
  }catch(e){ statusEl.textContent="Microphone blocked. Allow it in the browser's address bar and reload."; btn.disabled=false; return; }
  ctx = new AudioContext({sampleRate:24000});
  ws = new WebSocket((location.protocol==="https:"?"wss://":"ws://")+location.host+"/talk/ws?key="+encodeURIComponent(KEY));
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if(m.type==="ready"){ statusEl.textContent="Connected - start talking"; btn.textContent="Hang up"; btn.className="stop"; btn.disabled=false; }
    else if(m.type==="audio") play(unb64(m.audio));
    else if(m.type==="clear") stopPlayback();
    else if(m.type==="transcript") log(m.who==="caller"?"you":"agent",(m.who==="caller"?"You: ":"Agent: ")+m.text);
    else if(m.type==="tool") log("sys","[checking: "+m.name+"]");
    else if(m.type==="error") { log("sys","Error: "+m.message); }
  };
  ws.onclose = () => { statusEl.textContent="Disconnected"; cleanup(); };
  src = ctx.createMediaStreamSource(micStream);
  proc = ctx.createScriptProcessor(2048,1,1);
  proc.onaudioprocess = e => {
    if(!ws || ws.readyState!==1) return;
    const f=e.inputBuffer.getChannelData(0), i16=new Int16Array(f.length);
    for(let i=0;i<f.length;i++){ const v=Math.max(-1,Math.min(1,f[i])); i16[i]=v<0?v*0x8000:v*0x7fff; }
    ws.send(JSON.stringify({type:"audio",audio:b64(i16.buffer)}));
  };
  const mute=ctx.createGain(); mute.gain.value=0;   // keep the processor running without echoing the mic
  src.connect(proc); proc.connect(mute); mute.connect(ctx.destination);
}
function cleanup(){
  try{proc&&proc.disconnect()}catch(e){} try{src&&src.disconnect()}catch(e){}
  micStream&&micStream.getTracks().forEach(t=>t.stop()); stopPlayback();
  try{ctx&&ctx.close()}catch(e){} ws=null; btn.textContent="Start"; btn.className=""; btn.disabled=false;
}
btn.onclick = () => { if(ws){ ws.close(); } else { start(); } };
</script></body></html>"""


@app.get("/talk")
async def talk_page(request: Request):
    check_key(request)
    return HTMLResponse(_TALK_PAGE)


@app.websocket("/talk/ws")
async def talk_ws(browser_ws: WebSocket):
    await browser_ws.accept()
    if browser_ws.query_params.get("key") != TOOL_SECRET:
        await browser_ws.send_text(json.dumps({"type": "error", "message": "Wrong key in the page address."}))
        await browser_ws.close()
        return
    if not (AAI_API_KEY and AGENT_ID):
        await browser_ws.send_text(json.dumps({"type": "error",
            "message": "Add ASSEMBLYAI_API_KEY and AGENT_ID in Railway Variables, then redeploy."}))
        await browser_ws.close()
        return

    print("[talk] browser session started", flush=True)
    try:
        async with websockets.connect(
            AAI_WS_URL, additional_headers={"Authorization": f"Bearer {AAI_API_KEY}"}
        ) as aai_ws:
            await aai_ws.send(json.dumps({"type": "session.update", "session": {"agent_id": AGENT_ID}}))
            ready = asyncio.Event()

            async def browser_to_agent():
                while True:
                    msg = json.loads(await browser_ws.receive_text())
                    if msg.get("type") == "audio" and ready.is_set():
                        await aai_ws.send(json.dumps({"type": "input.audio", "audio": msg["audio"]}))

            async def agent_to_browser():
                async for raw in aai_ws:
                    msg = json.loads(raw)
                    kind = msg.get("type")
                    if kind == "session.ready":
                        ready.set()
                        await browser_ws.send_text(json.dumps({"type": "ready"}))
                        print("[talk] agent ready", flush=True)
                    elif kind == "reply.audio":
                        audio = msg.get("data") or msg.get("audio")
                        if audio:
                            await browser_ws.send_text(json.dumps({"type": "audio", "audio": audio}))
                    elif kind == "input.speech.started" or (
                            kind == "reply.done" and msg.get("status") == "interrupted"):
                        await browser_ws.send_text(json.dumps({"type": "clear"}))
                    elif kind in ("transcript.user", "transcript.agent"):
                        who = "caller" if kind == "transcript.user" else "agent"
                        text = msg.get("text", "")
                        print(f"[talk] {who}: {text}", flush=True)
                        await browser_ws.send_text(json.dumps({"type": "transcript", "who": who, "text": text}))
                    elif kind in ("tool.call", "tool_call"):
                        await browser_ws.send_text(json.dumps({"type": "tool", "name": msg.get("name", "a tool")}))
                    elif kind in ("session.error", "error"):
                        print(f"[talk] AssemblyAI error: {json.dumps(msg)[:500]}", flush=True)
                        await browser_ws.send_text(json.dumps({"type": "error",
                            "message": str(msg.get("message") or msg.get("error") or msg)[:300]}))
                    elif kind == "session.ended":
                        return

            tasks = [asyncio.create_task(browser_to_agent()), asyncio.create_task(agent_to_browser())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                if t.exception() and not isinstance(t.exception(), (WebSocketDisconnect, websockets.ConnectionClosed)):
                    print(f"[talk] bridge error: {t.exception()!r}", flush=True)
            try:
                await aai_ws.send(json.dumps({"type": "session.end"}))
            except websockets.ConnectionClosed:
                pass
    except websockets.InvalidStatus as e:
        note = (f"AssemblyAI refused the connection ({e.response.status_code}). "
                "Check ASSEMBLYAI_API_KEY and AGENT_ID, or set AAI_WS_URL to wss://agents.assemblyai.com/v1/ws.")
        print(f"[talk] {note}", flush=True)
        try:
            await browser_ws.send_text(json.dumps({"type": "error", "message": note}))
        except Exception:
            pass
    except Exception as e:
        print(f"[talk] error: {e!r}", flush=True)
    finally:
        print("[talk] browser session ended", flush=True)
        try:
            await browser_ws.close()
        except Exception:
            pass
