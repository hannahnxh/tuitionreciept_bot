#!/usr/bin/env python3
"""
Combined entrypoint: runs the Telegram bot (polling) and the web dashboard
(FastAPI/uvicorn) side by side in one process, sharing the same DATA_DIR.

They run as independent threads, each with their own asyncio event loop,
and only interact through the shared clients.json/config.json files on disk.
"""
import asyncio
import os
import threading

import bot as bot_module
from web.app import app as web_app


def run_bot():
    # A freshly spawned thread has no asyncio event loop by default in
    # Python 3.10+; PTB's run_polling() needs one already set for the
    # current thread, so create and register one before calling main().
    asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        bot_module.main()
    except Exception:
        bot_module.logger.exception("Telegram bot crashed")


if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True, name="telegram-bot").start()

    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(web_app, host="0.0.0.0", port=port)
