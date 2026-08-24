# 🤖 Tuition Receipt Bot — Setup Guide

## Prerequisites
- Python 3.10 or higher
- A Telegram account

---

## Step 1: Get your Telegram Bot Token

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Follow the prompts — choose a name (e.g. "My Tuition Bot") and a username (e.g. `mytuition_bot`)
4. BotFather will give you a **token** that looks like:
   ```
   123456789:ABCdefGHIjklMNOpqrsTUVwxyz
   ```
5. Copy and keep it safe.

---

## Step 2: Install dependencies

Open a terminal in this folder and run:

```bash
pip install -r requirements.txt
```

---

## Step 3: Set your bot token

**On Mac/Linux:**
```bash
export TELEGRAM_BOT_TOKEN="your_token_here"
```

**On Windows (Command Prompt):**
```cmd
set TELEGRAM_BOT_TOKEN=your_token_here
```

**On Windows (PowerShell):**
```powershell
$env:TELEGRAM_BOT_TOKEN="your_token_here"
```

> 💡 To avoid setting this every time, create a `.env` file in the same folder:
> ```
> TELEGRAM_BOT_TOKEN=your_token_here
> ```
> Then install `python-dotenv` and add `from dotenv import load_dotenv; load_dotenv()` at the top of `bot.py`.

---

## Step 4: Run the bot

```bash
python bot.py
```

You should see:
```
🤖 Bot is running... Press Ctrl+C to stop.
```

---

## Step 5: Use the bot

Open Telegram, find your bot by its username, and send `/start`.

**First time setup:**
1. Tap **⚙️ Settings** → Set your tutor name (appears on receipts)
2. Set your default payment instructions (e.g. `PayNow to 9123 4567, Ref: Student Name`)

**Adding a client:**
1. Tap **👤 Add Client**
2. Enter their name, contact, and hourly rate

**Generating a receipt:**
1. Tap **🧾 Generate Receipt**
2. Choose the client
3. Enter the month (e.g. `06/2025`)
4. Add sessions one by one (date + duration in hours)
5. Tap ✅ Done — the bot sends both a formatted receipt and a plain-text version you can copy and send to parents

---

## Data storage

- `clients.json` — all your client data
- `config.json` — your tutor name and default payment info

Both are saved locally in the same folder as `bot.py`. **Back them up** if needed.

---

## Keeping the bot running

To keep the bot running after you close the terminal:

**Mac/Linux** — run in background:
```bash
nohup python bot.py &
```

**Windows** — run as a background task or use Task Scheduler.

Or run it on a free cloud server like [Railway](https://railway.app) or [Render](https://render.com).
