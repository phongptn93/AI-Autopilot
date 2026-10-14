# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.11
ARG NODE_VERSION=22

# ── Node runtime, copied (not curl|bash-installed) into the final image ─────────
# The agent runs `npm`/`node` in the repos it works on (frontend builds and tests).
FROM node:${NODE_VERSION}-bookworm-slim AS node

# ── Build the wheel ────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-bookworm AS build
WORKDIR /src
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md ./
COPY ai_autopilot ./ai_autopilot
RUN python -m build --wheel --outdir /dist

# ── Runtime ────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
WORKDIR /app

# git is required by the executor.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \
    node --version && npm --version

# Dependencies first (pinned by the lock, cached until it changes), then the app.
COPY requirements-lock.txt ./
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir -c requirements-lock.txt /tmp/*.whl && rm /tmp/*.whl

# The Claude CLI ships inside claude-agent-sdk; expose that exact binary as `claude`
# (interactive sessions call it by name) so the CLI version is pinned with the SDK.
RUN CLI="$(python -c 'import claude_agent_sdk, pathlib; print(pathlib.Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")')" && \
    test -x "$CLI" && ln -s "$CLI" /usr/local/bin/claude && claude --version

# Run as a non-root user. The Claude CLI refuses --dangerously-skip-permissions
# (permission_mode "bypassPermissions") under root, and non-root is good practice.
RUN useradd --create-home --uid 10001 autopilot && \
    mkdir -p /data /app/logs && chown -R autopilot:autopilot /data /app
VOLUME ["/data", "/app/logs"]
USER autopilot
ENV HOME=/home/autopilot

ENV AUTOPILOT_DATABASE_URL=sqlite+aiosqlite:////data/autopilot.db \
    AUTOPILOT_HEALTH_PORT=5080 \
    PYTHONUNBUFFERED=1

EXPOSE 5080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:5080/livez').status==200 else 1)" || exit 1

ENTRYPOINT ["python", "-m", "ai_autopilot"]
