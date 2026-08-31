"""
Web dashboard for the Tuition Receipt Bot.

Reads/writes the exact same clients.json / config.json that bot.py uses
(via DATA_DIR), so the Telegram bot and this website always agree on data.
Does not import or touch anything Telegram-specific — bot.py is untouched.

Known limitation: reminders for clients added/edited here only get
(re-)scheduled in Telegram the next time the bot process restarts, since
only the running bot process holds the live job queue.
"""
import os
import sys
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Make sure the project root (where bot.py lives) is importable regardless
# of how this app is launched (uvicorn web.app:app, python main.py, etc.)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot  # noqa: E402  (shared data/session logic lives here)

from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _get_or_create_secret(filename: str, generator) -> str:
    """Persist an auto-generated secret in DATA_DIR so it survives restarts."""
    path = os.path.join(bot.DATA_DIR, filename)
    if os.path.exists(path):
        with open(path, "r") as f:
            return f.read().strip()
    value = generator()
    with open(path, "w") as f:
        f.write(value)
    return value


WEB_PASSWORD = os.environ.get("WEB_PASSWORD") or _get_or_create_secret(
    ".web_password", lambda: secrets.token_urlsafe(6)
)
WEB_SECRET_KEY = os.environ.get("WEB_SECRET_KEY") or _get_or_create_secret(
    ".web_secret", lambda: secrets.token_hex(32)
)
CALENDAR_TOKEN = os.environ.get("CALENDAR_TOKEN") or _get_or_create_secret(
    ".calendar_token", lambda: secrets.token_urlsafe(24)
)

if not os.environ.get("WEB_PASSWORD"):
    bot.logger.warning(
        "No WEB_PASSWORD set — generated one and saved it to %s: %s",
        os.path.join(bot.DATA_DIR, ".web_password"), WEB_PASSWORD,
    )

serializer = URLSafeTimedSerializer(WEB_SECRET_KEY, salt="tuitionbot-web-auth")

app = FastAPI(title="Tuition Receipt Bot")


# ── Auth ───────────────────────────────────────────────────────────────────

def _is_authed(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        serializer.loads(token, max_age=COOKIE_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def require_auth(request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="Not authenticated")


class LoginIn(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginIn, response: Response):
    if not secrets.compare_digest(body.password, WEB_PASSWORD):
        raise HTTPException(status_code=401, detail="Wrong password")
    token = serializer.dumps({"ok": True})
    response = JSONResponse({"ok": True})
    response.set_cookie(
        COOKIE_NAME, token, max_age=COOKIE_MAX_AGE,
        httponly=True, samesite="lax",
    )
    return response


@app.post("/api/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/api/me")
def me(request: Request):
    return {"authed": _is_authed(request)}


# ── Summaries ──────────────────────────────────────────────────────────────

def _month_sessions_active(client: dict, year: int, month: int) -> list:
    """Like bot._month_sessions, but stops counting sessions after the
    client's retirement date — past months are unaffected."""
    sessions = bot._month_sessions(client, year, month)
    retired = client.get("retired_date")
    if not retired:
        return sessions
    return [s for s in sessions if s["date_obj"] <= retired]


@app.get("/api/summary/week", dependencies=[Depends(require_auth)])
def summary_week():
    today = bot.today_local()
    monday = today - bot.timedelta(days=today.weekday())
    data = bot.load_data()
    active_clients = {cid: c for cid, c in data["clients"].items() if not c.get("retired_date")}
    entries = bot._week_sessions({"clients": active_clients}, monday)
    return {
        "monday": monday.isoformat(),
        "sunday": (monday + bot.timedelta(days=6)).isoformat(),
        "entries": [
            {"date": d.isoformat(), "time": t, "client_name": name, "hours": hrs}
            for d, t, name, hrs in entries
        ],
    }


@app.get("/api/summary/month", dependencies=[Depends(require_auth)])
def summary_month(year: Optional[int] = None, month: Optional[int] = None):
    today = bot.today_local()
    year = year or today.year
    month = month or today.month
    data = bot.load_data()
    total_hours = 0.0
    total_amount = 0.0
    clients_out = []
    for cid, client in data["clients"].items():
        sessions = _month_sessions_active(client, year, month)
        if not sessions:
            continue
        hours = sum(s["hours"] for s in sessions)
        rate = client.get("rate", 0)
        amount = hours * rate
        total_hours += hours
        total_amount += amount
        clients_out.append({
            "cid": cid, "name": client["name"], "sessions": len(sessions),
            "hours": hours, "amount": amount,
        })
    return {
        "year": year, "month": month,
        "clients": clients_out,
        "total_hours": total_hours, "total_amount": total_amount,
    }


@app.get("/api/summary/alltime", dependencies=[Depends(require_auth)])
def summary_alltime():
    """Lifetime totals, bounded by a user-set 'tracking_since' month (defaults
    to the current month, since there's no historical ledger to reconstruct
    from — see the docstring at the top of this file)."""
    cfg = bot.load_config()
    data = bot.load_data()
    today = bot.today_local()

    since_str = cfg.get("tracking_since")
    since = bot.date.fromisoformat(since_str) if since_str else bot.date(today.year, today.month, 1)

    total_hours = 0.0
    total_amount = 0.0
    y, m = since.year, since.month
    while (y, m) <= (today.year, today.month):
        for client in data["clients"].values():
            sessions = _month_sessions_active(client, y, m)
            hours = sum(s["hours"] for s in sessions)
            total_hours += hours
            total_amount += hours * client.get("rate", 0)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    return {
        "since": since.isoformat(),
        "total_hours": total_hours,
        "total_amount": total_amount,
        "active_students": len([c for c in data["clients"].values() if not c.get("retired_date")]),
    }


# ── Clients ────────────────────────────────────────────────────────────────

class ClientIn(BaseModel):
    name: str
    subject: str = ""
    parent_name: str = ""
    contact: str = ""
    location: str = ""
    rate: float
    schedule_day: Optional[int] = None      # 0=Mon .. 6=Sun, or null for none
    schedule_hours: Optional[float] = None
    schedule_time: Optional[str] = None      # "HH:MM" or null


@app.get("/api/clients", dependencies=[Depends(require_auth)])
def list_clients():
    data = bot.load_data()
    out = []
    for cid, c in data["clients"].items():
        out.append({
            "cid": cid, **c,
            "schedule_label": bot.schedule_label(c),
        })
    out.sort(key=lambda c: c["name"].lower())
    return out


@app.post("/api/clients", dependencies=[Depends(require_auth)])
def create_client(body: ClientIn):
    data = bot.load_data()
    cid = bot.next_client_id(data)
    data["clients"][cid] = body.model_dump()
    bot.save_data(data)
    return {"cid": cid, **data["clients"][cid]}


@app.put("/api/clients/{cid}", dependencies=[Depends(require_auth)])
def update_client(cid: str, body: ClientIn):
    data = bot.load_data()
    if cid not in data["clients"]:
        raise HTTPException(status_code=404, detail="Client not found")
    existing = data["clients"][cid]
    updated = body.model_dump()
    # preserve fields the web form doesn't manage
    for key in ("cancelled_dates", "rescheduled_dates", "extra_sessions", "retired_date"):
        if key in existing:
            updated[key] = existing[key]
    data["clients"][cid] = updated
    bot.save_data(data)
    return {"cid": cid, **updated}


@app.delete("/api/clients/{cid}", dependencies=[Depends(require_auth)])
def delete_client(cid: str):
    data = bot.load_data()
    if cid not in data["clients"]:
        raise HTTPException(status_code=404, detail="Client not found")
    del data["clients"][cid]
    bot.save_data(data)
    return {"ok": True}


@app.post("/api/clients/{cid}/retire", dependencies=[Depends(require_auth)])
def retire_client(cid: str):
    """Mark a client retired as of today. Past sessions/receipts/earnings are
    untouched — they just stop appearing in current/future views (dashboard,
    calendar feed, active-student count) going forward. Reversible."""
    data = bot.load_data()
    client = _get_client_or_404(data, cid)
    client["retired_date"] = bot.today_local().isoformat()
    bot.save_data(data)
    return {"cid": cid, **client}


@app.post("/api/clients/{cid}/reactivate", dependencies=[Depends(require_auth)])
def reactivate_client(cid: str):
    data = bot.load_data()
    client = _get_client_or_404(data, cid)
    client.pop("retired_date", None)
    bot.save_data(data)
    return {"cid": cid, **client}


# ── Session editing (cancel / reschedule / one-off sessions) ─────────────
# Writes the exact same clients.json fields bot.py's Reschedule / Add Session /
# Remove Session flows do, so either interface can pick up where the other left off.

def _get_client_or_404(data: dict, cid: str) -> dict:
    if cid not in data["clients"]:
        raise HTTPException(status_code=404, detail="Client not found")
    return data["clients"][cid]


def _client_month_detail(client: dict, year: int, month: int) -> list:
    """Every session in a month, including cancelled/rescheduled ones, for the editor UI."""
    cancelled_dates   = set(client.get("cancelled_dates", []))
    rescheduled_dates = client.get("rescheduled_dates", {})
    month_start = bot.date(year, month, 1)
    month_end   = bot.date(year, month, bot.calendar.monthrange(year, month)[1])

    rows = []
    if client.get("schedule_day") is not None:
        for d in bot.get_weekday_dates_in_month(year, month, client["schedule_day"]):
            d_iso = d.isoformat()
            if d_iso in rescheduled_dates:
                new_iso, rsc_time = bot._rsc_unpack(rescheduled_dates[d_iso])
                rows.append({
                    "date": d_iso, "hours": client["schedule_hours"], "time": client.get("schedule_time"),
                    "kind": "recurring", "status": "rescheduled_out",
                    "new_date": new_iso, "new_time": rsc_time,
                })
            elif d_iso in cancelled_dates:
                rows.append({
                    "date": d_iso, "hours": client["schedule_hours"], "time": client.get("schedule_time"),
                    "kind": "recurring", "status": "cancelled",
                })
            else:
                rows.append({
                    "date": d_iso, "hours": client["schedule_hours"], "time": client.get("schedule_time"),
                    "kind": "recurring", "status": "active",
                })

    # make-up sessions rescheduled INTO this month from another month
    for orig_iso, entry in rescheduled_dates.items():
        new_iso, rsc_time = bot._rsc_unpack(entry)
        new_d, orig_d = bot.date.fromisoformat(new_iso), bot.date.fromisoformat(orig_iso)
        if month_start <= new_d <= month_end and not (month_start <= orig_d <= month_end):
            rows.append({
                "date": new_iso, "hours": client.get("schedule_hours"),
                "time": rsc_time or client.get("schedule_time"),
                "kind": "recurring", "status": "rescheduled_in", "orig_date": orig_iso,
            })

    for ex in client.get("extra_sessions", []):
        ex_d = bot.date.fromisoformat(ex["date"])
        if month_start <= ex_d <= month_end:
            rows.append({
                "date": ex["date"], "hours": ex["hours"], "time": ex.get("time"),
                "kind": "extra", "status": "active",
            })

    retired = client.get("retired_date")
    if retired:
        rows = [r for r in rows if r["date"] <= retired]

    rows.sort(key=lambda r: r["date"])
    return rows


@app.get("/api/clients/{cid}/sessions", dependencies=[Depends(require_auth)])
def client_sessions(cid: str, year: int, month: int):
    data = bot.load_data()
    client = _get_client_or_404(data, cid)
    return _client_month_detail(client, year, month)


class DateIn(BaseModel):
    date: str  # ISO


class RescheduleIn(BaseModel):
    orig_date: str
    new_date: str
    time: Optional[str] = None


class ExtraSessionIn(BaseModel):
    date: str
    time: Optional[str] = None
    hours: float


@app.post("/api/clients/{cid}/cancel", dependencies=[Depends(require_auth)])
def cancel_session(cid: str, body: DateIn):
    data = bot.load_data()
    client = _get_client_or_404(data, cid)
    cancelled = client.setdefault("cancelled_dates", [])
    if body.date not in cancelled:
        cancelled.append(body.date)
    bot.save_data(data)
    return {"ok": True}


@app.post("/api/clients/{cid}/restore", dependencies=[Depends(require_auth)])
def restore_session(cid: str, body: DateIn):
    data = bot.load_data()
    client = _get_client_or_404(data, cid)
    client["cancelled_dates"] = [d for d in client.get("cancelled_dates", []) if d != body.date]
    bot.save_data(data)
    return {"ok": True}


@app.post("/api/clients/{cid}/reschedule", dependencies=[Depends(require_auth)])
def reschedule_session(cid: str, body: RescheduleIn):
    data = bot.load_data()
    client = _get_client_or_404(data, cid)
    client.setdefault("rescheduled_dates", {})[body.orig_date] = {
        "date": body.new_date, "time": body.time or client.get("schedule_time"),
    }
    bot.save_data(data)
    return {"ok": True}


@app.post("/api/clients/{cid}/undo-reschedule", dependencies=[Depends(require_auth)])
def undo_reschedule_session(cid: str, body: DateIn):
    data = bot.load_data()
    client = _get_client_or_404(data, cid)
    client.get("rescheduled_dates", {}).pop(body.date, None)
    bot.save_data(data)
    return {"ok": True}


@app.post("/api/clients/{cid}/extra-sessions", dependencies=[Depends(require_auth)])
def add_extra_session(cid: str, body: ExtraSessionIn):
    data = bot.load_data()
    client = _get_client_or_404(data, cid)
    client.setdefault("extra_sessions", []).append({
        "date": body.date, "time": body.time or "", "hours": body.hours,
    })
    bot.save_data(data)
    return {"ok": True}


@app.delete("/api/clients/{cid}/extra-sessions/{session_date}", dependencies=[Depends(require_auth)])
def remove_extra_session(cid: str, session_date: str):
    data = bot.load_data()
    client = _get_client_or_404(data, cid)
    client["extra_sessions"] = [ex for ex in client.get("extra_sessions", []) if ex["date"] != session_date]
    bot.save_data(data)
    return {"ok": True}


# ── Receipt (read-only) ───────────────────────────────────────────────────

@app.get("/api/receipt", dependencies=[Depends(require_auth)])
def receipt(cid: str, year: int, month: int):
    data = bot.load_data()
    if cid not in data["clients"]:
        raise HTTPException(status_code=404, detail="Client not found")
    client = data["clients"][cid]
    raw_sessions = _month_sessions_active(client, year, month)
    sessions = [
        {
            "date": bot.date.fromisoformat(s["date_obj"]).strftime("%d %b %Y"),
            "hours": s["hours"],
        }
        for s in sorted(raw_sessions, key=lambda s: s["date_obj"])
    ]
    cfg = bot.load_config()
    month_label = bot.date(year, month, 1).strftime("%B %Y")
    receipt_data = {
        "client": client,
        "sessions": sessions,
        "month_label": month_label,
        "payment_info": cfg.get("payment_info", ""),
    }
    text = bot.build_receipt_plain(receipt_data, cfg)
    return {"text": text}


# ── Settings ───────────────────────────────────────────────────────────────

class SettingsIn(BaseModel):
    tutor_name: str = ""
    payment_info: str = ""
    tracking_since: str = ""  # "YYYY-MM-DD", or "" to default to the current month


@app.get("/api/settings", dependencies=[Depends(require_auth)])
def get_settings():
    cfg = bot.load_config()
    today = bot.today_local()
    default_since = bot.date(today.year, today.month, 1).isoformat()
    return {
        "tutor_name": cfg.get("tutor_name", ""),
        "payment_info": cfg.get("payment_info", ""),
        "tracking_since": cfg.get("tracking_since", default_since),
    }


@app.post("/api/settings", dependencies=[Depends(require_auth)])
def save_settings(body: SettingsIn):
    cfg = bot.load_config()
    cfg["tutor_name"] = body.tutor_name
    cfg["payment_info"] = body.payment_info
    if body.tracking_since:
        cfg["tracking_since"] = body.tracking_since
    bot.save_config(cfg)
    return {"ok": True}


@app.get("/api/settings/calendar-url", dependencies=[Depends(require_auth)])
def get_calendar_url():
    return {"path": f"/calendar/{CALENDAR_TOKEN}.ics"}


# ── Calendar feed (read-only, one-way: app → calendar app) ───────────────
# Subscribed calendar apps fetch this URL directly (no cookies), so it's
# authenticated by an unguessable token in the path instead of the login
# session. Regenerated fresh on every request from current client data.

def _month_range(months_back: int, months_forward: int):
    today = bot.today_local()
    y, m = today.year, today.month
    for _ in range(months_back):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    total = months_back + months_forward + 1
    for _ in range(total):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _build_calendar_ics() -> bytes:
    import icalendar

    cal = icalendar.Calendar()
    cal.add("prodid", "-//Tuition Receipt Bot//tuition-schedule//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", "Tuition Schedule")
    cal.add("refresh-interval;value=duration", "PT4H")

    data = bot.load_data()
    seen = set()  # (cid, date) dedupe across the overlapping month windows

    for cid, client in data["clients"].items():
        for year, month in _month_range(months_back=3, months_forward=12):
            for row in _client_month_detail(client, year, month):
                if row["status"] not in ("active", "rescheduled_in"):
                    continue  # skip cancelled / moved-away rows entirely
                key = (cid, row["date"], row["kind"])
                if key in seen:
                    continue
                seen.add(key)

                d = bot.date.fromisoformat(row["date"])
                hours = row.get("hours") or 1
                time_str = row.get("time")

                summary_parts = ["Tuition"]
                if client.get("subject"):
                    summary_parts.append(client["subject"])
                summary_parts.append(client["name"])

                event = icalendar.Event()
                event.add("uid", f"{cid}-{row['date']}-{row['kind']}@tuitionbot")
                event.add("summary", " ".join(summary_parts))
                event.add("dtstamp", datetime.now(timezone.utc))
                if client.get("location"):
                    event.add("location", client["location"])

                if time_str:
                    t = datetime.strptime(time_str, "%H:%M").time()
                    start_local = datetime.combine(d, t, tzinfo=bot.TZ)
                    event.add("dtstart", start_local.astimezone(timezone.utc))
                    event.add("duration", bot.timedelta(hours=hours))
                else:
                    event.add("dtstart", d)  # all-day event
                    event.add("duration", bot.timedelta(days=1))

                desc_lines = [f"Rate: ${client.get('rate', 0):.2f}/hr", f"Hours: {hours}"]
                if row["status"] == "rescheduled_in":
                    desc_lines.append(f"Makeup for {row['orig_date']}")
                if row["kind"] == "extra":
                    desc_lines.append("One-off session")
                event.add("description", "\n".join(desc_lines))

                cal.add_component(event)

    return cal.to_ical()


@app.get("/calendar/{token}.ics")
def calendar_feed(token: str):
    if not secrets.compare_digest(token, CALENDAR_TOKEN):
        raise HTTPException(status_code=404)
    return Response(content=_build_calendar_ics(), media_type="text/calendar; charset=utf-8")


# ── Static frontend ────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/manifest.json")
def manifest():
    return FileResponse(str(STATIC_DIR / "manifest.json"), media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse(str(STATIC_DIR / "sw.js"), media_type="application/javascript")
