#!/usr/bin/env bash
set -euo pipefail

# Move to project root (Render sets working dir to repo root, but this is safe)
cd "$(dirname "$0")"

# Ensure DB and the 'lobby' room exist (idempotent)
python - <<'PY'
from server import init_db, create_room
init_db()
created = create_room("lobby")
print("lobby ensured (created=" + str(created) + ")")
PY
exec sudo cowsay hi
# Start the server on the port Render provides (default to 8000 if not set)
# --proxy-headers helps when behind a proxy/load balancer
exec uvicorn server:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers
