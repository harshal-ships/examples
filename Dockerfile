FROM node:24-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8080 \
    OPENCLAW_GATEWAY_PORT=8080 \
    OPENCLAW_STATE_DIR=/data/.openclaw \
    OPENCLAW_WORKSPACE_DIR=/data/workspace \
    OPENCLAW_CONFIG_PATH=/data/.openclaw/openclaw.json \
    BOOKINGS_PATH=/data/bookings.json

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g openclaw@latest

WORKDIR /app

COPY requirements.txt .
RUN python3 -m venv /opt/venv \
    && pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple telcoflow-sdk==0.27.1

COPY booking_agent.py reminder_agent.py render_start.sh ./
RUN chmod +x /app/render_start.sh

EXPOSE 8080
CMD ["/app/render_start.sh"]