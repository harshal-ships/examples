#!/usr/bin/env bash
set -euo pipefail

# Render provides PORT for web services; OpenClaw uses the same port for HTTP and WS.
export OPENCLAW_GATEWAY_PORT="${PORT:-${OPENCLAW_GATEWAY_PORT:-8080}}"
export OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/data/.openclaw}"
export OPENCLAW_WORKSPACE_DIR="${OPENCLAW_WORKSPACE_DIR:-/data/workspace}"
export OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-$OPENCLAW_STATE_DIR/openclaw.json}"

mkdir -p "$OPENCLAW_STATE_DIR" "$OPENCLAW_WORKSPACE_DIR"

# Render secret env vars are easier to manage than committing credentials files.
if [[ -n "${GOOGLE_CALENDAR_CREDENTIALS_JSON:-}" && -z "${GOOGLE_CALENDAR_CREDENTIALS:-}" ]]; then
  export GOOGLE_CALENDAR_CREDENTIALS="${GOOGLE_CALENDAR_CREDENTIALS_PATH:-/data/google-calendar-credentials.json}"
  python - "$GOOGLE_CALENDAR_CREDENTIALS" <<'PY'
import json
import os
import sys

target_path = sys.argv[1]
try:
    credentials = json.loads(os.environ["GOOGLE_CALENDAR_CREDENTIALS_JSON"])
except json.JSONDecodeError as exc:
    raise SystemExit(
        "GOOGLE_CALENDAR_CREDENTIALS_JSON must be one complete JSON object, "
        "not separate Render env vars for each JSON field."
    ) from exc

with open(target_path, "w", encoding="utf-8") as credentials_file:
    json.dump(credentials, credentials_file)
PY
  chmod 600 "$GOOGLE_CALENDAR_CREDENTIALS"
fi

# Create a minimal OpenClaw config on first boot; the persistent disk preserves edits.
cat > "$OPENCLAW_CONFIG_PATH" <<EOF
{
  "gateway": {
    "mode": "local",
    "port": ${OPENCLAW_GATEWAY_PORT},
    "bind": "lan",
    "trustedProxies": ["10.0.0.0/8"],
    "auth": {
      "token": "${OPENCLAW_GATEWAY_TOKEN}"
    },
    "controlUi": {
      "allowedOrigins": ["https://examples-n0xz.onrender.com"],
      "dangerouslyAllowHostHeaderOriginFallback": true
    }
  },
  "agents": {
    "defaults": {
      "workspace": "${OPENCLAW_WORKSPACE_DIR}",
      "model": {
        "primary": "google/gemini-2.5-flash"
      }
    },
    "list": [
      {
        "id": "main",
        "default": true,
        "workspace": "${OPENCLAW_WORKSPACE_DIR}"
      }
    ]
  }
}
EOF

# Start OpenClaw first so the Python scripts can use `openclaw agent` locally.
openclaw gateway --bind lan --port "$OPENCLAW_GATEWAY_PORT" --allow-unconfigured &
OPENCLAW_PID=$!

cleanup() {
  kill "$OPENCLAW_PID" 2>/dev/null || true
  if [[ -n "${BOOKING_PID:-}" ]]; then kill "$BOOKING_PID" 2>/dev/null || true; fi
  if [[ -n "${REMINDER_PID:-}" ]]; then kill "$REMINDER_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT INT TERM

sleep "${OPENCLAW_BOOT_DELAY_SECONDS:-5}"

# AGENT_MODE controls what this Render service runs:
# - gateway: OpenClaw only
# - booking: inbound booking agent plus OpenClaw
# - reminder: hourly reminder agent plus OpenClaw
# - both: booking and reminder in one container, sharing /data
case "${AGENT_MODE:-booking}" in
  gateway)
    wait "$OPENCLAW_PID"
    ;;
  booking)
    python /app/booking_agent.py &
    BOOKING_PID=$!
    wait -n "$OPENCLAW_PID" "$BOOKING_PID"
    ;;
  reminder)
    python /app/reminder_agent.py &
    REMINDER_PID=$!
    wait -n "$OPENCLAW_PID" "$REMINDER_PID"
    ;;
  both)
    python /app/booking_agent.py &
    BOOKING_PID=$!
    python /app/reminder_agent.py &
    REMINDER_PID=$!
    wait -n "$OPENCLAW_PID" "$BOOKING_PID" "$REMINDER_PID"
    ;;
  *)
    echo "Unknown AGENT_MODE: ${AGENT_MODE}" >&2
    exit 1
    ;;
esac
