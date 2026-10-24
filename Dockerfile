FROM node:22-slim

# Pin the Claude Code version (npm "stable" dist-tag as of 2026-10-24).
ARG CLAUDE_CODE_VERSION=2.1.308

# tzdata: node:22-slim ships no zoneinfo, and the 07:30 Europe/Stockholm
# schedule needs it to follow DST independently of the server clock.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 \
      python3-pip \
      ca-certificates \
      gosu \
      tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    && npm cache clean --force

WORKDIR /app

# Read by both bot.py and cli.py (whose defaults are for running from a checkout).
ENV CONFIG_PATH=/app/config/config.json \
    DATA_DIR=/data

COPY requirements.txt /app/requirements.txt
RUN python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY --chown=node:node app/ /app/

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Starts as root so the entrypoint can fix volume ownership, then drops to
# `node` via gosu before running anything that touches network input.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python3", "/app/bot.py"]
