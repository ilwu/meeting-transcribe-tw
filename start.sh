#!/bin/sh
# Start the transcript proofreading tool and open the browser.
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
exec "$PY" app.py --open
