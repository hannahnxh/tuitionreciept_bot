#!/usr/bin/env python3
"""
Entrypoint: runs the web dashboard only.

The Telegram bot is no longer run here — `bot.py` is kept around purely as
a shared library (data loading, session-date math, receipt formatting) that
web/app.py imports from, so client/session data stays computed the same way
it always was. If you ever want the Telegram bot back, run `python bot.py`
directly (it still works standalone) and this file's docstring history has
the notes on running both together in one process.
"""
import os

import uvicorn

from web.app import app as web_app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(web_app, host="0.0.0.0", port=port, loop="asyncio")
