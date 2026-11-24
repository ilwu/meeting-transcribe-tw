#!/bin/sh
# Update to the latest version: git pull, reinstall packages if the list changed, run the self-check.
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
# exec: the shell is replaced before git pull can change this file under it.
exec "$PY" utils/update.py
