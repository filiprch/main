#!/usr/bin/env bash
# Start the dashboard.
#
#   ./start.sh              → http://127.0.0.1:5000
#   PORT=5050 ./start.sh    → use a different port if 5000 is busy
#
# Creates the virtual environment and installs dependencies when they are
# missing, so this also repairs the venv after the project folder is moved
# (a moved venv keeps absolute paths and silently stops working).
set -e
cd "$(dirname "$0")"

if [ ! -x venv/bin/python ]; then
    echo "→ Creating virtual environment..."
    rm -rf venv
    python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate

if ! python -c "import flask, psycopg2, dotenv, requests_aws4auth" 2>/dev/null; then
    echo "→ Installing dependencies..."
    pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt
fi

if [ ! -f .env ]; then
    echo "!  No .env file found — copy .env.example to .env and fill it in."
fi

echo "→ Dashboard on http://127.0.0.1:${PORT:-5000}   (Ctrl+C to stop)"
exec python dashboard.py
