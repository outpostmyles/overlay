#!/usr/bin/env bash
# Launch the poly dashboard. Creates a venv on first run, installs deps, starts the server.
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "→ creating virtualenv…"
  python3 -m venv .venv
fi
source .venv/bin/activate

echo "→ installing dependencies…"
pip install -q -r requirements.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "→ created .env (optional: add your free The Odds API key to enable sportsbook lines)"
fi

PORT="${PORT:-8000}"
echo "→ dashboard at http://localhost:${PORT}"
exec uvicorn backend.main:app --host 127.0.0.1 --port "${PORT}" "$@"
