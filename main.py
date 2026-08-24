#!/usr/bin/env python3
"""
Combined entrypoint: runs the Telegram bot (polling) and the web dashboard
(FastAPI/uvicorn) side by side in one process, sharing the same DATA_DIR.

The bot runs in the *main* thread (unchanged from standalone `python bot.py`)
because python-telegram-bot's run_polling() installs OS signal handlers,
which Python only allows from the main thread of the main interpreter —
there's no way to do that from a background thread. uvicorn doesn't have
that restriction (it just skips signal setup when not in the main thread),
so the web server is what runs in the background thread instead.

They only interact through the shared clients.json/config.json files on disk.
"""
import os
import threading

import uvicorn

import bot as bot_module
from web.app import app as web_app


def run_web():
    port = int(os.environ.get("PORT", 8000))
    try:
        # uvicorn defaults to uvloop when available, which replaces the
        # process-wide asyncio event loop policy — that then breaks PTB's
        # run_polling() in the main thread, which expects the stock asyncio
        # policy. Force the standard loop so the web server's presence
        # doesn't change how the bot's own event loop behaves.
        uvicorn.run(web_app, host="0.0.0.0", port=port, loop="asyncio")
    except Exception:
        bot_module.logger.exception("Web server crashed")


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True, name="web-server").start()
    bot_module.main()  # blocks in the main thread, same as standalone `python bot.py`
