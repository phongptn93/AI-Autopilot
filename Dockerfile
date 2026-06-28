FROM python:3.11-slim AS runtime

WORKDIR /app

# git is required by the executor; Node.js backs the Claude Code CLI used by the SDK.
RUN apt-get update && apt-get install -y --no-install-recommends curl git ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g @anthropic-ai/claude-code && \
    apt-get purge -y curl && apt-get autoremove -y && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY ai_autopilot ./ai_autopilot
RUN pip install --no-cache-dir .

# Runtime data directories.
RUN mkdir -p /data /app/logs
VOLUME ["/data", "/app/logs"]

ENV AUTOPILOT_DATABASE_URL=sqlite+aiosqlite:////data/autopilot.db \
    AUTOPILOT_HEALTH_PORT=5080

EXPOSE 5080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:5080/health').status==200 else 1)" || exit 1

ENTRYPOINT ["python", "-m", "ai_autopilot"]
