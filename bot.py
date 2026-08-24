#!/usr/bin/env python3
"""
Tuition Receipt Telegram Bot v2
- Recurring weekly schedule per client (stored)
- Auto-generates session dates for a given month
- Inline edit/remove per session before generating receipt
"""

import json
import os
import logging
import calendar
from datetime import datetime, date, timedelta
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from dotenv import load_dotenv; 

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR = os.environ.get("DATA_DIR", ".")
DATA_FILE  = os.path.join(DATA_DIR, "clients.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY_SHORT    = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# ── Conversation states ───────────────────────────────────────────────────────
(
    ADD_NAME, ADD_PARENT_NAME, ADD_CONTACT, ADD_LOCATION, ADD_RATE, ADD_SCHEDULE_DAY, ADD_SCHEDULE_HOURS,
    EDIT_CHOOSE_FIELD, EDIT_VALUE,
    RECEIPT_CHOOSE_CLIENT, RECEIPT_MONTH_YEAR, RECEIPT_REVIEW_SESSIONS,
    RECEIPT_RESCHEDULE_DATE, RECEIPT_SESSION_HOURS_OVERRIDE,
    RECEIPT_PAYMENT_INFO,
    SETUP_VALUE,
    ADD_SCHEDULE_TIME,
    RESCHEDULE_CHOOSE_CLIENT, RESCHEDULE_ORIG_DATE, RESCHEDULE_NEW_DATE, RESCHEDULE_NEW_TIME,
    ADDSESS_CHOOSE_CLIENT, ADDSESS_DATE, ADDSESS_TIME, ADDSESS_HOURS,
    REMSESS_CHOOSE_CLIENT, REMSESS_PICK,
) = range(27)


# ══════════════════════════════════════════════════════════════════════════════
#  DATA HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"clients": {}}

def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}

def save_config(config: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

def next_client_id(data: dict) -> str:
    ids = [int(k) for k in data["clients"].keys()] if data["clients"] else [0]
    return str(max(ids) + 1)


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_weekday_dates_in_month(year: int, month: int, weekday: int) -> list[date]:
    """Return all dates in given month that fall on `weekday` (0=Mon … 6=Sun)."""
    first = date(year, month, 1)
    last  = date(year, month, calendar.monthrange(year, month)[1])
    result = []
    current = first
    # advance to first occurrence of weekday
    days_ahead = (weekday - first.weekday()) % 7
    current = first + timedelta(days=days_ahead)
    while current <= last:
        result.append(current)
        current += timedelta(weeks=1)
    return result

def schedule_label(client: dict) -> str:
    day = client.get("schedule_day")
    hrs = client.get("schedule_hours")
    t   = client.get("schedule_time")
    if day is None:
        return "No recurring schedule"
    time_part = f" at {t}" if t else ""
    return f"Every {DAYS_OF_WEEK[day]}{time_part}, {hrs} hr{'s' if hrs != 1 else ''}"


# ── Reminder job helpers ──────────────────────────────────────────────────────

def _seconds_until_next(weekday: int, time_str: str) -> float:
    """Seconds from now until 10 min before the next occurrence of weekday+time."""
    t = datetime.strptime(time_str, "%H:%M").time()
    now = datetime.now()
    target_today = datetime.combine(now.date(), t) - timedelta(minutes=10)
    days_ahead = (weekday - now.weekday()) % 7
    if days_ahead == 0 and now >= target_today:
        days_ahead = 7
    target = datetime.combine(now.date() + timedelta(days=days_ahead), t) - timedelta(minutes=10)
    return max((target - now).total_seconds(), 1)

def schedule_reminder_job(app, cid: str, client: dict, chat_id: int):
    """Register (or replace) a weekly 10-min-before reminder for a client."""
    if app.job_queue is None:
        logger.warning("JobQueue not available — install python-telegram-bot[job-queue]")
        return
    day      = client.get("schedule_day")
    time_str = client.get("schedule_time")
    if day is None or not time_str:
        return
    for job in app.job_queue.get_jobs_by_name(f"reminder_{cid}"):
        job.schedule_removal()
    secs = _seconds_until_next(day, time_str)
    fires_at = datetime.now() + timedelta(seconds=secs)
    app.job_queue.run_repeating(
        reminder_job,
        interval=timedelta(weeks=1),
        first=secs,
        name=f"reminder_{cid}",
        data={"cid": cid, "chat_id": chat_id, "client_name": client["name"], "time": time_str},
    )
    logger.info("Reminder scheduled for %s (cid=%s): first fires at %s",
                client["name"], cid, fires_at.strftime("%Y-%m-%d %H:%M"))

async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    logger.info("Firing reminder for %s (cid=%s)", d["client_name"], d["cid"])
    # session date is ~10 min from now
    session_date = (datetime.now() + timedelta(minutes=11)).date().isoformat()
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("Going ahead", callback_data=f"remind_yes_{d['cid']}_{session_date}"),
        InlineKeyboardButton("Cancelled",   callback_data=f"remind_no_{d['cid']}_{session_date}"),
    ]])
    await context.bot.send_message(
        chat_id=d["chat_id"],
        text=f"Session with *{d['client_name']}* starts in 10 minutes ({d['time']})!\n\nIs this session going ahead?",
        parse_mode="Markdown",
        reply_markup=buttons,
    )

async def reminder_confirm_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # remind_yes_<cid>_<date> or remind_no_<cid>_<date>
    _, action, cid, session_date = query.data.split("_", 3)
    data = load_data()
    if cid not in data["clients"]:
        await query.edit_message_text("Client not found.")
        return
    name = data["clients"][cid]["name"]
    if action == "yes":
        await query.edit_message_text(f"Got it — session with *{name}* is going ahead!", parse_mode="Markdown")
    else:
        cancelled = data["clients"][cid].setdefault("cancelled_dates", [])
        if session_date not in cancelled:
            cancelled.append(session_date)
            save_data(data)
        await query.edit_message_text(f"Session with *{name}* on {session_date} marked as cancelled.", parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN MENU
# ══════════════════════════════════════════════════════════════════════════════

MAIN_MENU = [
    ["Add Client", "List of Clients"],
    ["Generate Receipt", "Reschedule"],
    ["Add Session", "Remove Session"],
    ["Settings"],
]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    config["chat_id"] = update.effective_chat.id
    save_config(config)
    tutor = config.get("tutor_name", "")
    greeting = f"Hello, *{tutor}*! \n\n" if tutor else "Welcome to your *Tuition Receipt Bot*! \n\n"
    await update.message.reply_text(
        greeting +
        "Here's what I can do:\n"
        "• Add & manage clients with a *recurring weekly schedule*\n"
        "• Auto-generate session dates for any month\n"
        "• Edit, reschedule, or cancel individual sessions\n"
        "• Generate a text receipt to send to parents\n\n"
        "Pick an option below.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True),
    )
    return ConversationHandler.END

async def test_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a test reminder for every client immediately."""
    cfg     = load_config()
    chat_id = cfg.get("chat_id")
    if not chat_id:
        await update.message.reply_text("No chat_id saved — send /start first.")
        return
    data    = load_data()
    if not data["clients"]:
        await update.message.reply_text("No clients.")
        return
    for cid, client in data["clients"].items():
        time_str = client.get("schedule_time", "??:??")
        context.application.job_queue.run_once(
            reminder_job,
            when=2,
            data={"cid": cid, "chat_id": chat_id, "client_name": client["name"], "time": time_str},
        )
    await update.message.reply_text(f"Test reminders firing in 2 seconds for {len(data['clients'])} client(s).")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Action cancelled.",
        reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True),
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  ADD CLIENT
# ══════════════════════════════════════════════════════════════════════════════

async def add_client_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Add New Client*\n\nWhat is the client's *full name*?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["Cancel"]], resize_keyboard=True),
    )
    return ADD_NAME

async def add_client_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_client"] = {"name": update.message.text.strip()}
    await update.message.reply_text(
        f"Student: *{context.user_data['new_client']['name']}*\n\n"
        "What is the *parent's name*?\n_(Type `skip` to leave blank)_",
        parse_mode="Markdown",
    )
    return ADD_PARENT_NAME

async def add_client_parent_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["new_client"]["parent_name"] = "" if text.lower() == "skip" else text
    await update.message.reply_text(
        "What is their *contact number or email*?\n_(Type `skip` to leave blank)_",
        parse_mode="Markdown",
    )
    return ADD_CONTACT

async def add_client_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["new_client"]["contact"] = "" if text.lower() == "skip" else text
    await update.message.reply_text(
        "Where does the student stay? _(e.g. Jurong West, Tampines)_\n_(Type `skip` to leave blank)_",
        parse_mode="Markdown",
    )
    return ADD_LOCATION

async def add_client_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["new_client"]["location"] = "" if text.lower() == "skip" else text
    await update.message.reply_text(
        "What is the *hourly rate* (SGD)?\nJust the number, e.g. `50` for $50/hr",
        parse_mode="Markdown",
    )
    return ADD_RATE

async def add_client_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace("$", "")
    try:
        rate = float(text)
        assert rate > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("Please enter a valid number, e.g. `50`", parse_mode="Markdown")
        return ADD_RATE

    context.user_data["new_client"]["rate"] = rate

    # Offer day-of-week buttons
    day_buttons = [[InlineKeyboardButton(d, callback_data=f"daypick_{i}") for i, d in enumerate(DAY_SHORT[:4])],
                   [InlineKeyboardButton(d, callback_data=f"daypick_{i+4}") for i, d in enumerate(DAY_SHORT[4:])],
                   [InlineKeyboardButton("No recurring schedule", callback_data="daypick_none")],
                   [InlineKeyboardButton("Exit", callback_data="daypick_exit")]]
    await update.message.reply_text(
        "Which *day of the week* does this student have tuition?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(day_buttons),
    )
    return ADD_SCHEDULE_DAY

async def add_schedule_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    val = query.data.split("_", 1)[1]

    if val == "exit":
        context.user_data.clear()
        await query.edit_message_text("Action cancelled.")
        await query.message.reply_text(
            "What's next?",
            reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True),
        )
        return ConversationHandler.END

    if val == "none":
        context.user_data["new_client"]["schedule_day"] = None
        context.user_data["new_client"]["schedule_hours"] = None
        await _save_new_client(query, context)
        return ConversationHandler.END

    context.user_data["new_client"]["schedule_day"] = int(val)
    day_name = DAYS_OF_WEEK[int(val)]
    await query.edit_message_text(
        f"Every *{day_name}* — how many *hours* per session?\ne.g. `1.5` or `2`",
        parse_mode="Markdown",
    )
    return ADD_SCHEDULE_HOURS

async def add_schedule_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "new_client" not in context.user_data:
        await update.message.reply_text(
            "Session expired. Please start over.",
            reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True),
        )
        return ConversationHandler.END

    text = update.message.text.strip()
    try:
        hours = float(text)
        assert hours > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("Enter a positive number, e.g. `1.5`", parse_mode="Markdown")
        return ADD_SCHEDULE_HOURS

    context.user_data["new_client"]["schedule_hours"] = hours
    await update.message.reply_text(
        "What time do sessions start? (used for 10-min reminders)\n"
        "Format: `HH:MM` (24hr), e.g. `14:30`\n\nSend `skip` to set this later.",
        parse_mode="Markdown",
    )
    return ADD_SCHEDULE_TIME

async def add_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "new_client" not in context.user_data:
        await update.message.reply_text(
            "Session expired. Please start over.",
            reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True),
        )
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.lower() != "skip":
        try:
            datetime.strptime(text, "%H:%M")
            context.user_data["new_client"]["schedule_time"] = text
        except ValueError:
            await update.message.reply_text("Use 24hr format, e.g. `14:30`  (or send `skip`)", parse_mode="Markdown")
            return ADD_SCHEDULE_TIME

    await _save_new_client(update.message, context)
    return ConversationHandler.END

async def _save_new_client(source, context: ContextTypes.DEFAULT_TYPE):
    data   = load_data()
    cid    = next_client_id(data)
    client = context.user_data["new_client"]
    data["clients"][cid] = client
    save_data(data)

    cfg     = load_config()
    chat_id = cfg.get("chat_id")
    if chat_id:
        schedule_reminder_job(context.application, cid, client, chat_id)

    context.user_data.clear()

    sched = schedule_label(client)
    text = (
        f"*Client added!*\n\n"
        f"Student: {client['name']}\n"
        f"Parent: {client.get('parent_name') or '—'}\n"
        f"Contact: {client.get('contact') or '—'}\n"
        f"Location: {client.get('location') or '—'}\n"
        f"Rate: ${client['rate']:.2f}/hr\n"
        f"Schedule: {sched}"
    )
    if hasattr(source, "edit_message_text"):  # callback query
        await source.edit_message_text(text, parse_mode="Markdown")
        await source.message.reply_text("What's next?",
            reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True))
    else:                                     # regular message
        await source.reply_text(text, parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True))


# ══════════════════════════════════════════════════════════════════════════════
#  LIST CLIENTS
# ══════════════════════════════════════════════════════════════════════════════

async def list_clients(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    if not data["clients"]:
        await update.message.reply_text(
            "No clients yet. Use *Add Client* to get started!",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True),
        )
        return

    lines = ["*Your Clients*\n"]
    for cid, c in data["clients"].items():
        parent   = f" (parent: {c['parent_name']})" if c.get("parent_name") else ""
        contact  = f" · {c['contact']}"             if c.get("contact")     else ""
        location = f" · {c['location']}"            if c.get("location")    else ""
        lines.append(
            f"*{c['name']}*{parent}{contact}{location}\n"
            f"   ${c['rate']:.2f}/hr  |  {schedule_label(c)}\n"
        )

    buttons = []
    for cid, c in data["clients"].items():
        buttons.append([
            InlineKeyboardButton(f"Edit {c['name']}", callback_data=f"edit_{cid}"),
            InlineKeyboardButton("Remove", callback_data=f"remove_{cid}"),
        ])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

async def remove_client_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cid  = query.data.split("_", 1)[1]
    data = load_data()
    if cid in data["clients"]:
        name = data["clients"][cid]["name"]
        del data["clients"][cid]
        save_data(data)
        await query.edit_message_text(f"*{name}* has been removed.", parse_mode="Markdown")
    else:
        await query.edit_message_text("Client not found.")

async def edit_client_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cid  = query.data.split("_", 1)[1]
    data = load_data()
    if cid not in data["clients"]:
        await query.edit_message_text("Client not found.")
        return ConversationHandler.END

    context.user_data["edit_cid"] = cid
    c = data["clients"][cid]
    buttons = [
        [InlineKeyboardButton("Student Name", callback_data="efield_name"),
         InlineKeyboardButton("Parent Name",  callback_data="efield_parent_name")],
        [InlineKeyboardButton("Contact",  callback_data="efield_contact"),
         InlineKeyboardButton("Location", callback_data="efield_location")],
        [InlineKeyboardButton("Rate", callback_data="efield_rate")],
        [InlineKeyboardButton("Schedule Day", callback_data="efield_schedule_day")],
        [InlineKeyboardButton("Schedule Hours", callback_data="efield_schedule_hours"),
         InlineKeyboardButton("Session Time",   callback_data="efield_schedule_time")],
    ]
    await query.edit_message_text(
        f"Editing *{c['name']}*. Which field?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_CHOOSE_FIELD

async def edit_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    field = query.data.split("_", 1)[1]
    context.user_data["edit_field"] = field

    if field == "schedule_day":
        day_buttons = [
            [InlineKeyboardButton(d, callback_data=f"edaypick_{i}") for i, d in enumerate(DAY_SHORT[:4])],
            [InlineKeyboardButton(d, callback_data=f"edaypick_{i+4}") for i, d in enumerate(DAY_SHORT[4:])],
            [InlineKeyboardButton("No recurring schedule", callback_data="edaypick_none")],
            [InlineKeyboardButton("Exit", callback_data="edaypick_exit")],
        ]
        await query.edit_message_text("Choose the new *schedule day*:", parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup(day_buttons))
        return EDIT_CHOOSE_FIELD   # stays in same state, handled by edaypick_ callback

    labels = {
        "name": "full name",
        "contact": "contact / email (or `skip`)",
        "parent_name": "parent's name (or `skip` to clear)",
        "location": "location (or `skip` to clear)",
        "rate": "hourly rate (number only)",
        "schedule_hours": "hours per session (number)",
        "schedule_time": "session start time in 24hr format, e.g. `14:30` (or `skip` to clear)",
    }
    await query.edit_message_text(f"Enter the new *{labels[field]}*:", parse_mode="Markdown")
    return EDIT_VALUE

async def edit_daypick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    val = query.data.split("_", 1)[1]

    if val == "exit":
        context.user_data.clear()
        await query.edit_message_text("Action cancelled.")
        await query.message.reply_text(
            "What's next?",
            reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True),
        )
        return ConversationHandler.END

    cid = context.user_data.get("edit_cid")
    data = load_data()
    data["clients"][cid]["schedule_day"] = None if val == "none" else int(val)
    save_data(data)

    cfg     = load_config()
    chat_id = cfg.get("chat_id")
    if chat_id:
        schedule_reminder_job(context.application, cid, data["clients"][cid], chat_id)

    context.user_data.clear()
    label = "None" if val == "none" else DAYS_OF_WEEK[int(val)]
    await query.edit_message_text(f"Schedule day updated to *{label}*.", parse_mode="Markdown")
    return ConversationHandler.END

async def edit_value_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("edit_field")
    cid   = context.user_data.get("edit_cid")
    text  = update.message.text.strip()
    data  = load_data()

    if field in ("rate", "schedule_hours"):
        try:
            value = float(text.replace("$", ""))
            assert value > 0
        except (ValueError, AssertionError):
            await update.message.reply_text("Enter a valid positive number.")
            return EDIT_VALUE
    elif field == "schedule_time":
        if text.lower() == "skip":
            value = None
        else:
            try:
                datetime.strptime(text, "%H:%M")
                value = text
            except ValueError:
                await update.message.reply_text("Use 24hr format, e.g. `14:30`  (or `skip` to clear)", parse_mode="Markdown")
                return EDIT_VALUE
    elif field in ("contact", "location", "parent_name") and text.lower() == "skip":
        value = ""
    else:
        value = text

    data["clients"][cid][field] = value
    save_data(data)
    c = data["clients"][cid]

    if field in ("schedule_time", "schedule_day"):
        cfg     = load_config()
        chat_id = cfg.get("chat_id")
        if chat_id:
            schedule_reminder_job(context.application, cid, c, chat_id)

    context.user_data.clear()
    await update.message.reply_text(
        f"Updated! *{c['name']}*:\n"
        f"{c.get('contact') or '—'}  |  💰 ${c['rate']:.2f}/hr\n"
        f"{schedule_label(c)}",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True),
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  GENERATE RECEIPT — NEW FLOW
#
#  1. Choose client
#  2. Enter month/year
#  3. Bot auto-generates recurring dates → show list with ✏️ Reschedule / 🗑 Remove per date
#  4. User can also ➕ Add manual session
#  5. Confirm → payment info → receipt
# ══════════════════════════════════════════════════════════════════════════════

def _sessions_message(sessions: list) -> str:
    if not sessions:
        return "_No sessions yet._"
    lines = []
    for i, s in enumerate(sessions):
        if s.get("cancelled"):
            note = f"moved to {s['rescheduled_to']}" if s.get("rescheduled_to") else "cancelled"
            status = f"~~{note}~~"
        else:
            hrs = s["hours"]
            status = f"{hrs} hr{'s' if hrs != 1 else ''}"
            if s.get("rescheduled_from"):
                status += f" _(rescheduled from {s['rescheduled_from']})_"
        lines.append(f"{i+1}. {s['date']}  —  {status}")
    return "\n".join(lines)

def _sessions_buttons(sessions: list) -> InlineKeyboardMarkup:
    """One row per session: [Reschedule] [Remove]  +  done + add buttons."""
    buttons = []
    for i, s in enumerate(sessions):
        if s.get("cancelled"):
            buttons.append([InlineKeyboardButton(f"Restore #{i+1}", callback_data=f"srestore_{i}")])
        else:
            buttons.append([
                InlineKeyboardButton(f"#{i+1} Reschedule", callback_data=f"sreschedule_{i}"),
                InlineKeyboardButton(f"#{i+1} Hours",      callback_data=f"shours_{i}"),
                InlineKeyboardButton(f"#{i+1} Remove",     callback_data=f"sremove_{i}"),
            ])
    buttons.append([InlineKeyboardButton("Add a session", callback_data="sadd")])
    buttons.append([InlineKeyboardButton("Done — Generate Receipt", callback_data="sdone")])
    buttons.append([InlineKeyboardButton("Exit", callback_data="sexit")])
    return InlineKeyboardMarkup(buttons)

async def receipt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    if not data["clients"]:
        await update.message.reply_text(
            "No clients yet. Please add a client first.",
            reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True),
        )
        return ConversationHandler.END

    buttons = [[InlineKeyboardButton(c["name"], callback_data=f"rc_{cid}")]
               for cid, c in data["clients"].items()]
    await update.message.reply_text(
        "*Generate Receipt*\n\nChoose a client:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return RECEIPT_CHOOSE_CLIENT

async def receipt_client_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cid  = query.data.split("_", 1)[1]
    data = load_data()
    context.user_data["receipt"] = {
        "cid": cid,
        "client": data["clients"][cid],
        "sessions": [],
    }
    await query.edit_message_text(
        f"Client: *{data['clients'][cid]['name']}*\n\n"
        "Which *month and year* is this receipt for?\n"
        "Format: `MM/YYYY`  e.g. `04/2026`",
        parse_mode="Markdown",
    )
    return RECEIPT_MONTH_YEAR

async def receipt_month_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        dt = datetime.strptime(text, "%m/%Y")
    except ValueError:
        await update.message.reply_text("Use format `MM/YYYY`, e.g. `04/2026`", parse_mode="Markdown")
        return RECEIPT_MONTH_YEAR

    receipt = context.user_data["receipt"]
    receipt["month_label"] = dt.strftime("%B %Y")
    client  = receipt["client"]

    # Auto-generate dates from recurring schedule
    cancelled_dates   = set(client.get("cancelled_dates", []))
    rescheduled_dates = client.get("rescheduled_dates", {})
    month_start = date(dt.year, dt.month, 1)
    month_end   = date(dt.year, dt.month, calendar.monthrange(dt.year, dt.month)[1])

    def _rsc_unpack(entry):
        """Return (new_iso, time_str) from a rescheduled_dates entry (old str or new dict)."""
        if isinstance(entry, dict):
            return entry["date"], entry.get("time")
        return entry, None  # backwards compat: old plain-string format

    sessions = []
    if client.get("schedule_day") is not None:
        dates = get_weekday_dates_in_month(dt.year, dt.month, client["schedule_day"])
        for d in dates:
            d_iso = d.isoformat()
            if d_iso in rescheduled_dates:
                new_iso, rsc_time = _rsc_unpack(rescheduled_dates[d_iso])
                new_d = date.fromisoformat(new_iso)
                time_note = f" at {rsc_time}" if rsc_time and rsc_time != client.get("schedule_time") else ""
                if month_start <= new_d <= month_end:
                    sessions.append({
                        "date": new_d.strftime("%d %b %Y") + time_note,
                        "date_obj": new_iso,
                        "hours": client["schedule_hours"],
                        "cancelled": False,
                        "rescheduled_from": d.strftime("%d %b %Y"),
                    })
                else:
                    sessions.append({
                        "date": d.strftime("%d %b %Y"),
                        "date_obj": d_iso,
                        "hours": client["schedule_hours"],
                        "cancelled": True,
                        "rescheduled_to": new_d.strftime("%d %b %Y") + time_note,
                    })
            else:
                sessions.append({
                    "date": d.strftime("%d %b %Y"),
                    "date_obj": d_iso,
                    "hours": client["schedule_hours"],
                    "cancelled": d_iso in cancelled_dates,
                })

    # Add make-up sessions rescheduled INTO this month from another month
    for orig_iso, entry in rescheduled_dates.items():
        new_iso, rsc_time = _rsc_unpack(entry)
        orig_d = date.fromisoformat(orig_iso)
        new_d  = date.fromisoformat(new_iso)
        time_note = f" at {rsc_time}" if rsc_time and rsc_time != client.get("schedule_time") else ""
        if month_start <= new_d <= month_end and not (month_start <= orig_d <= month_end):
            sessions.append({
                "date": new_d.strftime("%d %b %Y") + time_note,
                "date_obj": new_iso,
                "hours": client["schedule_hours"],
                "cancelled": False,
                "rescheduled_from": orig_d.strftime("%d %b %Y"),
            })

    # Add manually added one-off sessions in this month
    for ex in client.get("extra_sessions", []):
        ex_d = date.fromisoformat(ex["date"])
        if month_start <= ex_d <= month_end:
            time_note = f" at {ex['time']}" if ex.get("time") and ex["time"] != client.get("schedule_time") else ""
            sessions.append({
                "date": ex_d.strftime("%d %b %Y") + time_note,
                "date_obj": ex["date"],
                "hours": ex["hours"],
                "cancelled": False,
            })

    sessions.sort(key=lambda s: s["date_obj"])
    receipt["sessions"] = sessions

    day_name = DAYS_OF_WEEK[client["schedule_day"]] if client.get("schedule_day") is not None else None
    if sessions:
        intro = (
            f"*{receipt['month_label']}* — found *{len(sessions)} {day_name}s*.\n\n"
            "Review the sessions below. Use the buttons to reschedule, remove, or add sessions.\n\n"
        )
    else:
        intro = (
            f"*{receipt['month_label']}*\n\n"
            "No recurring schedule set for this client (or no matching days in this month).\n"
            "Use Add a session to add sessions manually.\n\n"
        )

    await update.message.reply_text(
        intro + _sessions_message(sessions),
        parse_mode="Markdown",
        reply_markup=_sessions_buttons(sessions),
    )
    return RECEIPT_REVIEW_SESSIONS


# ── Session inline actions ────────────────────────────────────────────────────

async def session_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("_")[1])
    sessions = context.user_data["receipt"]["sessions"]
    sessions[idx]["cancelled"] = True
    await query.edit_message_text(
        f"Session #{idx+1} removed.\n\n" + _sessions_message(sessions),
        parse_mode="Markdown",
        reply_markup=_sessions_buttons(sessions),
    )
    return RECEIPT_REVIEW_SESSIONS

async def session_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("_")[1])
    sessions = context.user_data["receipt"]["sessions"]
    sessions[idx]["cancelled"] = False
    await query.edit_message_text(
        f"Session #{idx+1} restored.\n\n" + _sessions_message(sessions),
        parse_mode="Markdown",
        reply_markup=_sessions_buttons(sessions),
    )
    return RECEIPT_REVIEW_SESSIONS

async def session_reschedule_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("_")[1])
    context.user_data["reschedule_idx"] = idx
    sessions = context.user_data["receipt"]["sessions"]
    await query.edit_message_text(
        f"Rescheduling session #{idx+1} (*{sessions[idx]['date']}*).\n\n"
        "Enter the *new date*:\nFormat: `DD/MM/YYYY`",
        parse_mode="Markdown",
    )
    return RECEIPT_RESCHEDULE_DATE

async def session_reschedule_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        dt = datetime.strptime(text, "%d/%m/%Y")
    except ValueError:
        await update.message.reply_text("Use format `DD/MM/YYYY`, e.g. `12/04/2026`", parse_mode="Markdown")
        return RECEIPT_RESCHEDULE_DATE

    idx = context.user_data.pop("reschedule_idx")
    sessions = context.user_data["receipt"]["sessions"]
    old_date = sessions[idx]["date"]
    sessions[idx]["date"]     = dt.strftime("%d %b %Y")
    sessions[idx]["date_obj"] = dt.isoformat()
    # Re-sort sessions by date
    sessions.sort(key=lambda s: s["date_obj"])

    await update.message.reply_text(
        f"Session rescheduled from *{old_date}* → *{dt.strftime('%d %b %Y')}*\n\n"
        + _sessions_message(sessions),
        parse_mode="Markdown",
        reply_markup=_sessions_buttons(sessions),
    )
    return RECEIPT_REVIEW_SESSIONS

async def session_hours_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("_")[1])
    context.user_data["hours_idx"] = idx
    sessions = context.user_data["receipt"]["sessions"]
    await query.edit_message_text(
        f"⏱ Session #{idx+1} (*{sessions[idx]['date']}*) — currently *{sessions[idx]['hours']} hrs*.\n\n"
        "Enter the new *duration in hours*:",
        parse_mode="Markdown",
    )
    return RECEIPT_SESSION_HOURS_OVERRIDE

async def session_hours_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        hours = float(text)
        assert hours > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("Enter a positive number, e.g. `1.5`", parse_mode="Markdown")
        return RECEIPT_SESSION_HOURS_OVERRIDE

    idx = context.user_data.pop("hours_idx")
    sessions = context.user_data["receipt"]["sessions"]
    sessions[idx]["hours"] = hours
    await update.message.reply_text(
        f"Hours updated.\n\n" + _sessions_message(sessions),
        parse_mode="Markdown",
        reply_markup=_sessions_buttons(sessions),
    )
    return RECEIPT_REVIEW_SESSIONS

async def session_add_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["adding_manual"] = True
    await query.edit_message_text(
        "*Add a session*\n\nEnter the *date*:\nFormat: `DD/MM/YYYY`",
        parse_mode="Markdown",
    )
    return RECEIPT_RESCHEDULE_DATE   # reuse the date-entry state

async def session_add_date_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles both reschedule and add-manual paths."""
    if context.user_data.get("adding_manual"):
        text = update.message.text.strip()
        try:
            dt = datetime.strptime(text, "%d/%m/%Y")
        except ValueError:
            await update.message.reply_text("Use format `DD/MM/YYYY`", parse_mode="Markdown")
            return RECEIPT_RESCHEDULE_DATE

        client  = context.user_data["receipt"]["client"]
        default_hours = client.get("schedule_hours") or 1.0
        context.user_data["manual_date"] = dt
        context.user_data["hours_idx"]   = None   # sentinel for add path

        await update.message.reply_text(
            f"*{dt.strftime('%d %b %Y')}* — how many *hours*? (default: {default_hours})\n"
            f"Type a number or send `default` to use {default_hours} hrs.",
            parse_mode="Markdown",
        )
        return RECEIPT_SESSION_HOURS_OVERRIDE
    else:
        return await session_reschedule_receive(update, context)

async def session_add_hours_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles both hours-override and add-manual-hours paths."""
    if context.user_data.get("adding_manual"):
        text = update.message.text.strip()
        client = context.user_data["receipt"]["client"]
        default_hours = client.get("schedule_hours") or 1.0
        if text.lower() == "default":
            hours = default_hours
        else:
            try:
                hours = float(text)
                assert hours > 0
            except (ValueError, AssertionError):
                await update.message.reply_text("Enter a positive number or `default`", parse_mode="Markdown")
                return RECEIPT_SESSION_HOURS_OVERRIDE

        dt = context.user_data.pop("manual_date")
        context.user_data.pop("adding_manual", None)
        sessions = context.user_data["receipt"]["sessions"]
        sessions.append({
            "date": dt.strftime("%d %b %Y"),
            "date_obj": dt.isoformat(),
            "hours": hours,
            "cancelled": False,
        })
        sessions.sort(key=lambda s: s["date_obj"])
        await update.message.reply_text(
            f"Session added: *{dt.strftime('%d %b %Y')}* — {hours} hrs\n\n"
            + _sessions_message(sessions),
            parse_mode="Markdown",
            reply_markup=_sessions_buttons(sessions),
        )
        return RECEIPT_REVIEW_SESSIONS
    else:
        return await session_hours_receive(update, context)


async def session_exit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("Receipt generation cancelled.")
    await query.message.reply_text(
        "What's next?",
        reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True),
    )
    return ConversationHandler.END

# ── Done → payment info ───────────────────────────────────────────────────────

async def session_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sessions = [s for s in context.user_data["receipt"]["sessions"] if not s.get("cancelled")]
    if not sessions:
        await query.edit_message_text(
            "No sessions to bill! Please add at least one session.",
            reply_markup=_sessions_buttons(context.user_data["receipt"]["sessions"]),
        )
        return RECEIPT_REVIEW_SESSIONS

    context.user_data["receipt"]["sessions"] = sessions  # keep only active ones

    config = load_config()
    default_payment = config.get("payment_info", "")
    prompt = "Enter *payment instructions* for this receipt:\n_(e.g. PayNow to 9123 4567, Ref: Name)_"
    if default_payment:
        prompt += f"\n\nOr send `skip` to use saved default:\n`{default_payment}`"

    await query.edit_message_text(prompt, parse_mode="Markdown")
    return RECEIPT_PAYMENT_INFO

async def receipt_payment_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text   = update.message.text.strip()
    config = load_config()

    if text.lower() == "skip":
        payment_info = config.get("payment_info", "")
    else:
        payment_info = text
        config["payment_info"] = payment_info
        save_config(config)

    receipt_data = context.user_data["receipt"]
    receipt_data["payment_info"] = payment_info

    formatted = build_receipt(receipt_data, config)
    plain     = build_receipt_plain(receipt_data, config)

    await update.message.reply_text(
        "*Receipt generated!*\n\n" + formatted,
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True),
    )
    await update.message.reply_text(
        "*Plain text* (copy & send to parent):\n```\n" + plain + "\n```",
        parse_mode="Markdown",
    )
    context.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  RECEIPT BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def build_receipt(receipt_data: dict, config: dict) -> str:
    client       = receipt_data["client"]
    sessions     = receipt_data["sessions"]
    rate         = client["rate"]
    total_hours  = sum(s["hours"] for s in sessions)
    total_amount = total_hours * rate
    tutor        = config.get("tutor_name", "")
    payment_info = receipt_data.get("payment_info", "")
    month        = receipt_data.get("month_label", "")
    parent       = client.get("parent_name", "")

    greeting = f"Hi *{parent}*," if parent else "Hi,"
    lines = [
        f"{greeting}",
        f"Here is the tuition receipt for *{client['name']}* for the month of *{month}*.\n",
    ]
    if tutor:
        lines.append(f"*Tutor:* {tutor}")
    lines.append(f"*Rate:* ${rate:.2f}/hr\n")
    lines.append("*Sessions:*")
    for s in sessions:
        lines.append(f"  • {s['date']} — {s['hours']} hr{'s' if s['hours'] != 1 else ''}")
    lines.append(f"\n*Total Hours:* {total_hours} hrs")
    lines.append(f"*Total Amount: ${total_amount:.2f}*")
    if payment_info:
        lines.append(f"\n*Payment:* {payment_info}")
    lines.append(f"\n_Thank you! Generated {datetime.now().strftime('%d %b %Y')}_")
    return "\n".join(lines)

def build_receipt_plain(receipt_data: dict, config: dict) -> str:
    client       = receipt_data["client"]
    sessions     = receipt_data["sessions"]
    rate         = client["rate"]
    total_hours  = sum(s["hours"] for s in sessions)
    total_amount = total_hours * rate
    tutor        = config.get("tutor_name", "")
    payment_info = receipt_data.get("payment_info", "")
    month        = receipt_data.get("month_label", "")
    parent       = client.get("parent_name", "")
    sep          = "-" * 34

    greeting = f"Hi {parent}," if parent else "Hi,"
    lines = [
        greeting,
        f"Here is the tuition receipt for {client['name']} for the month of {month}.",
        "",
        sep,
    ]
    if tutor:
        lines.append(f"Tutor   : {tutor}")
    lines.append(f"Rate    : ${rate:.2f} per hour")
    lines += ["", "Sessions:"]
    for s in sessions:
        lines.append(f"  {s['date']}  ({s['hours']} hr{'s' if s['hours'] != 1 else ''})")
    lines += [
        sep,
        f"Total Hours : {total_hours} hrs",
        f"Total Amount: ${total_amount:.2f}",
    ]
    if payment_info:
        lines += ["", f"Payment : {payment_info}"]
    lines += [sep, "", f"Thank you! Generated: {datetime.now().strftime('%d %b %Y')}"]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  RESCHEDULE
# ══════════════════════════════════════════════════════════════════════════════

async def reschedule_start(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    if not data["clients"]:
        await update.message.reply_text(
            "No clients yet.",
            reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True),
        )
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(c["name"], callback_data=f"rsc_{cid}")]
               for cid, c in data["clients"].items()]
    await update.message.reply_text(
        "Which client are you rescheduling a session for?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return RESCHEDULE_CHOOSE_CLIENT

async def reschedule_client_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cid  = query.data.split("_", 1)[1]
    data = load_data()
    client = data["clients"][cid]
    context.user_data["rsc_cid"] = cid

    existing = client.get("rescheduled_dates", {})
    info = ""
    if existing:
        lines = [f"  {date.fromisoformat(o).strftime('%d %b %Y')} → {date.fromisoformat(n).strftime('%d %b %Y')}"
                 for o, n in existing.items()]
        info = "\n\n*Existing reschedules:*\n" + "\n".join(lines)

    await query.edit_message_text(
        f"*{client['name']}* — {schedule_label(client)}{info}\n\n"
        "Enter the *original session date* to reschedule:\nFormat: `DD/MM/YYYY`",
        parse_mode="Markdown",
    )
    return RESCHEDULE_ORIG_DATE

async def reschedule_orig_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        d = datetime.strptime(text, "%d/%m/%Y").date()
    except ValueError:
        await update.message.reply_text("Use format `DD/MM/YYYY`, e.g. `03/05/2026`", parse_mode="Markdown")
        return RESCHEDULE_ORIG_DATE
    context.user_data["rsc_orig"] = d.isoformat()
    await update.message.reply_text(
        f"Moving *{d.strftime('%d %b %Y')}* — what is the *new date*?\nFormat: `DD/MM/YYYY`",
        parse_mode="Markdown",
    )
    return RESCHEDULE_NEW_DATE

async def reschedule_new_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        new_d = datetime.strptime(text, "%d/%m/%Y").date()
    except ValueError:
        await update.message.reply_text("Use format `DD/MM/YYYY`, e.g. `10/05/2026`", parse_mode="Markdown")
        return RESCHEDULE_NEW_DATE

    context.user_data["rsc_new_date"] = new_d.isoformat()
    cid      = context.user_data["rsc_cid"]
    data     = load_data()
    default  = data["clients"][cid].get("schedule_time", "")
    default_hint = f" (or `same` to keep {default})" if default else ""
    await update.message.reply_text(
        f"What time is the rescheduled session?\nFormat: `HH:MM` (24hr){default_hint}",
        parse_mode="Markdown",
    )
    return RESCHEDULE_NEW_TIME

async def reschedule_new_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    cid     = context.user_data.pop("rsc_cid")
    orig_iso = context.user_data.pop("rsc_orig")
    new_iso  = context.user_data.pop("rsc_new_date")
    new_d    = date.fromisoformat(new_iso)

    data   = load_data()
    client = data["clients"][cid]
    default_time = client.get("schedule_time", "")

    if text.lower() == "same":
        time_str = default_time
    else:
        try:
            datetime.strptime(text, "%H:%M")
            time_str = text
        except ValueError:
            await update.message.reply_text("Use 24hr format, e.g. `14:30`  (or `same`)", parse_mode="Markdown")
            # restore popped keys so the handler can retry
            context.user_data["rsc_cid"]      = cid
            context.user_data["rsc_orig"]     = orig_iso
            context.user_data["rsc_new_date"] = new_iso
            return RESCHEDULE_NEW_TIME

    client.setdefault("rescheduled_dates", {})[orig_iso] = {
        "date": new_iso,
        "time": time_str,
    }
    save_data(data)
    context.user_data.clear()

    # One-off reminder at the new date/time
    reminder_note = ""
    cfg     = load_config()
    chat_id = cfg.get("chat_id")
    if time_str and chat_id and context.application.job_queue:
        session_dt  = datetime.combine(new_d, datetime.strptime(time_str, "%H:%M").time())
        reminder_dt = session_dt - timedelta(minutes=10)
        if reminder_dt > datetime.now():
            context.application.job_queue.run_once(
                reminder_job,
                when=(reminder_dt - datetime.now()).total_seconds(),
                name=f"reminder_once_{cid}_{new_iso}",
                data={"cid": cid, "chat_id": chat_id, "client_name": client["name"], "time": time_str},
            )
            reminder_note = f"\nReminder set for {new_d.strftime('%d %b %Y')} at {time_str}."

    orig_label = date.fromisoformat(orig_iso).strftime("%d %b %Y")
    time_label = f" at {time_str}" if time_str else ""
    await update.message.reply_text(
        f"Done! *{orig_label}* → *{new_d.strftime('%d %b %Y')}{time_label}* saved.{reminder_note}\n"
        "This will be applied automatically when you generate the receipt.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True),
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  ADD SESSION
# ══════════════════════════════════════════════════════════════════════════════

async def addsess_start(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    if not data["clients"]:
        await update.message.reply_text("No clients yet.", reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True))
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(c["name"], callback_data=f"as_{cid}")]
               for cid, c in data["clients"].items()]
    await update.message.reply_text("Which client is this session for?",
                                    reply_markup=InlineKeyboardMarkup(buttons))
    return ADDSESS_CHOOSE_CLIENT

async def addsess_client_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cid = query.data.split("_", 1)[1]
    context.user_data["as_cid"] = cid
    await query.edit_message_text(
        "Enter the *date* of the extra session:\nFormat: `DD/MM/YYYY`",
        parse_mode="Markdown",
    )
    return ADDSESS_DATE

async def addsess_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        d = datetime.strptime(text, "%d/%m/%Y").date()
    except ValueError:
        await update.message.reply_text("Use format `DD/MM/YYYY`, e.g. `10/05/2026`", parse_mode="Markdown")
        return ADDSESS_DATE
    context.user_data["as_date"] = d.isoformat()
    cid     = context.user_data["as_cid"]
    default = load_data()["clients"][cid].get("schedule_time", "")
    hint    = f" (or `same` to use {default})" if default else ""
    await update.message.reply_text(
        f"What time does this session start?\nFormat: `HH:MM`{hint}",
        parse_mode="Markdown",
    )
    return ADDSESS_TIME

async def addsess_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    cid     = context.user_data["as_cid"]
    default = load_data()["clients"][cid].get("schedule_time", "")
    if text.lower() == "same":
        time_str = default
    else:
        try:
            datetime.strptime(text, "%H:%M")
            time_str = text
        except ValueError:
            await update.message.reply_text("Use 24hr format, e.g. `14:30`  (or `same`)", parse_mode="Markdown")
            return ADDSESS_TIME
    context.user_data["as_time"] = time_str
    default_hrs = load_data()["clients"][cid].get("schedule_hours", 1.0)
    await update.message.reply_text(
        f"How many *hours*? (or `same` to use {default_hrs})",
        parse_mode="Markdown",
    )
    return ADDSESS_HOURS

async def addsess_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    cid  = context.user_data.pop("as_cid")
    d_iso = context.user_data.pop("as_date")
    time_str = context.user_data.pop("as_time", "")
    data = load_data()
    client = data["clients"][cid]
    default_hrs = client.get("schedule_hours", 1.0)

    if text.lower() == "same":
        hours = default_hrs
    else:
        try:
            hours = float(text)
            assert hours > 0
        except (ValueError, AssertionError):
            await update.message.reply_text("Enter a positive number or `same`", parse_mode="Markdown")
            context.user_data["as_cid"]  = cid
            context.user_data["as_date"] = d_iso
            context.user_data["as_time"] = time_str
            return ADDSESS_HOURS

    client.setdefault("extra_sessions", []).append({
        "date": d_iso, "time": time_str, "hours": hours,
    })
    save_data(data)
    context.user_data.clear()

    # One-off reminder
    reminder_note = ""
    cfg     = load_config()
    chat_id = cfg.get("chat_id")
    if time_str and chat_id and context.application.job_queue:
        session_dt  = datetime.combine(date.fromisoformat(d_iso), datetime.strptime(time_str, "%H:%M").time())
        reminder_dt = session_dt - timedelta(minutes=10)
        if reminder_dt > datetime.now():
            context.application.job_queue.run_once(
                reminder_job,
                when=(reminder_dt - datetime.now()).total_seconds(),
                name=f"reminder_once_{cid}_{d_iso}",
                data={"cid": cid, "chat_id": chat_id, "client_name": client["name"], "time": time_str},
            )
            reminder_note = f"\nReminder set for {date.fromisoformat(d_iso).strftime('%d %b %Y')} at {time_str}."

    d_label = date.fromisoformat(d_iso).strftime("%d %b %Y")
    await update.message.reply_text(
        f"Added session: *{d_label}* — {hours} hr{'s' if hours != 1 else ''}.{reminder_note}",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True),
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  REMOVE SESSION
# ══════════════════════════════════════════════════════════════════════════════

def _next_n_sessions(client: dict, n: int = 8) -> list[date]:
    """Return the next n upcoming recurring session dates from today."""
    day = client.get("schedule_day")
    if day is None:
        return []
    today = date.today()
    results = []
    d = today + timedelta(days=(day - today.weekday()) % 7)
    while len(results) < n:
        results.append(d)
        d += timedelta(weeks=1)
    return results

async def remsess_start(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    if not data["clients"]:
        await update.message.reply_text("No clients yet.", reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True))
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(c["name"], callback_data=f"rs_{cid}")]
               for cid, c in data["clients"].items()]
    await update.message.reply_text("Which client's session are you removing?",
                                    reply_markup=InlineKeyboardMarkup(buttons))
    return REMSESS_CHOOSE_CLIENT

async def remsess_client_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cid    = query.data.split("_", 1)[1]
    data   = load_data()
    client = data["clients"][cid]
    context.user_data["rs_cid"] = cid

    upcoming   = _next_n_sessions(client)
    cancelled  = set(client.get("cancelled_dates", []))
    extra_isos = {ex["date"] for ex in client.get("extra_sessions", [])}

    buttons = []
    for d in upcoming:
        d_iso = d.isoformat()
        label = d.strftime("%d %b %Y") + (" (already removed)" if d_iso in cancelled else "")
        buttons.append([InlineKeyboardButton(label, callback_data=f"rspick_{d_iso}")])
    for d_iso in extra_isos:
        d_label = date.fromisoformat(d_iso).strftime("%d %b %Y") + " (added session)"
        buttons.append([InlineKeyboardButton(d_label, callback_data=f"rspick_extra_{d_iso}")])
    buttons.append([InlineKeyboardButton("Cancel", callback_data="rspick_cancel")])

    await query.edit_message_text(
        f"*{client['name']}* — pick the session to remove:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return REMSESS_PICK

async def remsess_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cid  = context.user_data.pop("rs_cid")
    val  = query.data[len("rspick_"):]  # strip prefix

    if val == "cancel":
        context.user_data.clear()
        await query.edit_message_text("Cancelled.")
        await query.message.reply_text("What's next?", reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True))
        return ConversationHandler.END

    data   = load_data()
    client = data["clients"][cid]

    if val.startswith("extra_"):
        d_iso = val[len("extra_"):]
        client["extra_sessions"] = [ex for ex in client.get("extra_sessions", []) if ex["date"] != d_iso]
        save_data(data)
        label = date.fromisoformat(d_iso).strftime("%d %b %Y")
        await query.edit_message_text(f"Removed added session on *{label}*.", parse_mode="Markdown")
    else:
        d_iso = val
        cancelled = client.setdefault("cancelled_dates", [])
        if d_iso not in cancelled:
            cancelled.append(d_iso)
            save_data(data)
        label = date.fromisoformat(d_iso).strftime("%d %b %Y")
        await query.edit_message_text(f"Session on *{label}* removed. It will be excluded from the receipt.", parse_mode="Markdown")

    context.user_data.clear()
    await query.message.reply_text("What's next?", reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True))
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

async def settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config  = load_config()
    tutor   = config.get("tutor_name",  "Not set")
    payment = config.get("payment_info","Not set")
    buttons = [
        [InlineKeyboardButton("Tutor Name",        callback_data="cfg_tutor")],
        [InlineKeyboardButton("Default Payment Info", callback_data="cfg_payment")],
    ]
    await update.message.reply_text(
        f"*Settings*\n\nTutor name: `{tutor}`\nPayment info: `{payment}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key   = query.data.split("_", 1)[1]
    context.user_data["cfg_key"] = key
    prompts = {
        "tutor":   "Enter your *tutor/business name*:",
        "payment": "Enter your *default payment instructions*:\ne.g. `PayNow to 9123 4567, Ref: Your Name`",
    }
    await query.edit_message_text(prompts[key], parse_mode="Markdown")
    return SETUP_VALUE

async def settings_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key   = context.user_data.get("cfg_key")
    config = load_config()
    config_keys = {"tutor": "tutor_name", "payment": "payment_info"}
    config[config_keys[key]] = update.message.text.strip()
    save_config(config)
    context.user_data.clear()
    await update.message.reply_text(
        "Setting saved!",
        reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True),
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  APP SETUP
# ══════════════════════════════════════════════════════════════════════════════

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("Set the TELEGRAM_BOT_TOKEN environment variable!")

    app = Application.builder().token(token).build()

    # ── Re-register reminder jobs from saved client data ──────────────────────
    cfg     = load_config()
    chat_id = cfg.get("chat_id")
    if chat_id:
        for cid, client in load_data()["clients"].items():
            schedule_reminder_job(app, cid, client, chat_id)

    # ── Add Client ────────────────────────────────────────────────────────────
    add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Add Client"), add_client_start)],
        states={
            ADD_NAME:           [MessageHandler(filters.TEXT & ~filters.COMMAND, add_client_name)],
            ADD_PARENT_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, add_client_parent_name)],
            ADD_CONTACT:        [MessageHandler(filters.TEXT & ~filters.COMMAND, add_client_contact)],
            ADD_LOCATION:       [MessageHandler(filters.TEXT & ~filters.COMMAND, add_client_location)],
            ADD_RATE:           [MessageHandler(filters.TEXT & ~filters.COMMAND, add_client_rate)],
            ADD_SCHEDULE_DAY:   [CallbackQueryHandler(add_schedule_day, pattern=r"^daypick_")],
            ADD_SCHEDULE_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_schedule_hours)],
            ADD_SCHEDULE_TIME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_schedule_time)],
        },
        fallbacks=[MessageHandler(filters.Regex("Cancel"), cancel)],
    )

    # ── Edit Client ───────────────────────────────────────────────────────────
    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_client_callback, pattern=r"^edit_")],
        states={
            EDIT_CHOOSE_FIELD: [
                CallbackQueryHandler(edit_field_callback,   pattern=r"^efield_"),
                CallbackQueryHandler(edit_daypick_callback, pattern=r"^edaypick_"),
            ],
            EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # ── Receipt ───────────────────────────────────────────────────────────────
    receipt_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Generate Receipt"), receipt_start)],
        states={
            RECEIPT_CHOOSE_CLIENT: [CallbackQueryHandler(receipt_client_chosen, pattern=r"^rc_")],
            RECEIPT_MONTH_YEAR:    [MessageHandler(filters.TEXT & ~filters.COMMAND, receipt_month_year)],
            RECEIPT_REVIEW_SESSIONS: [
                CallbackQueryHandler(session_remove,           pattern=r"^sremove_"),
                CallbackQueryHandler(session_restore,          pattern=r"^srestore_"),
                CallbackQueryHandler(session_reschedule_prompt,pattern=r"^sreschedule_"),
                CallbackQueryHandler(session_hours_prompt,     pattern=r"^shours_"),
                CallbackQueryHandler(session_add_prompt,       pattern=r"^sadd$"),
                CallbackQueryHandler(session_done,             pattern=r"^sdone$"),
                CallbackQueryHandler(session_exit,             pattern=r"^sexit$"),
            ],
            RECEIPT_RESCHEDULE_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, session_add_date_receive),
            ],
            RECEIPT_SESSION_HOURS_OVERRIDE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, session_add_hours_receive),
            ],
            RECEIPT_PAYMENT_INFO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receipt_payment_info),
            ],
        },
        fallbacks=[MessageHandler(filters.Regex("Cancel"), cancel)],
    )

    # ── Settings ──────────────────────────────────────────────────────────────
    settings_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Settings"), settings_start)],
        states={
            SETUP_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    # ── Reschedule ────────────────────────────────────────────────────────────
    reschedule_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Reschedule"), reschedule_start)],
        states={
            RESCHEDULE_CHOOSE_CLIENT: [CallbackQueryHandler(reschedule_client_chosen, pattern=r"^rsc_")],
            RESCHEDULE_ORIG_DATE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, reschedule_orig_date)],
            RESCHEDULE_NEW_DATE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, reschedule_new_date)],
            RESCHEDULE_NEW_TIME:      [MessageHandler(filters.TEXT & ~filters.COMMAND, reschedule_new_time)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # ── Add Session ───────────────────────────────────────────────────────────
    addsess_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Add Session"), addsess_start)],
        states={
            ADDSESS_CHOOSE_CLIENT: [CallbackQueryHandler(addsess_client_chosen, pattern=r"^as_")],
            ADDSESS_DATE:          [MessageHandler(filters.TEXT & ~filters.COMMAND, addsess_date)],
            ADDSESS_TIME:          [MessageHandler(filters.TEXT & ~filters.COMMAND, addsess_time)],
            ADDSESS_HOURS:         [MessageHandler(filters.TEXT & ~filters.COMMAND, addsess_hours)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # ── Remove Session ────────────────────────────────────────────────────────
    remsess_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Remove Session"), remsess_start)],
        states={
            REMSESS_CHOOSE_CLIENT: [CallbackQueryHandler(remsess_client_chosen, pattern=r"^rs_")],
            REMSESS_PICK:          [CallbackQueryHandler(remsess_pick,          pattern=r"^rspick_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("testreminder", test_reminder))
    app.add_handler(add_conv)
    app.add_handler(edit_conv)
    app.add_handler(receipt_conv)
    app.add_handler(reschedule_conv)
    app.add_handler(addsess_conv)
    app.add_handler(remsess_conv)
    app.add_handler(settings_conv)
    app.add_handler(MessageHandler(filters.Regex("List of Clients"), list_clients))
    app.add_handler(CallbackQueryHandler(remove_client_callback,   pattern=r"^remove_"))
    app.add_handler(CallbackQueryHandler(settings_callback,        pattern=r"^cfg_"))
    app.add_handler(CallbackQueryHandler(reminder_confirm_callback, pattern=r"^remind_"))

    print("Bot is running... Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    import asyncio
    import sys
    if sys.version_info >= (3, 12):
        asyncio.set_event_loop(asyncio.new_event_loop())
    main()
