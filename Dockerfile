# The Sales Agent as a container: the same Python and packages on the laptop (Docker Desktop) and the server.
# Build:  docker compose build        Run:  docker compose up -d        (see docs/how-to-use.md, "Running on the server")

# Official Python image, slim Debian. It is published for both amd64 (laptop) and arm64 (the Oracle server), and
# Docker picks the right one for the machine that builds it. 3.13 matches the laptop venv.
FROM python:3.13-slim-bookworm

# Print logs at once (no buffering, so `docker compose logs` is live), write no .pyc files, keep no pip cache.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HEARTBEAT_DIR=/data

# Time zone data, so log times can be shown in IST (TZ is set in docker-compose.yml). About 3 MB.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

# Everything lives in /app inside the container.
WORKDIR /app

# Copy the package description first: this layer and the install below are re-used from cache when only code changes.
COPY pyproject.toml ./
COPY src/ src/
# Editable install keeps the code at /app/src, so the agent finds icp.yaml and assets/ next to it as on the laptop.
# All dependencies ship ready-made wheels for arm64 and amd64, so no compiler is needed.
RUN pip install -e .

# Business rules and the signature logo. .env is NOT copied: secrets reach the container only when it starts.
COPY icp.yaml ./
COPY assets/ assets/

# Run as an ordinary user, not root: if the code were ever tricked into running something, it cannot change the image.
# /data holds the heartbeat files and the start log (a Docker volume keeps it across restarts).
RUN useradd --uid 10001 --create-home agent && mkdir /data && chown agent /data
USER agent

# Every minute Docker asks "are the loops alive?". Two minutes' grace at start; three failures mark it unhealthy.
# (The agent's own watchdog restarts it on a hang; this makes the state visible in `docker compose ps`.)
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 CMD ["python", "-m", "zypher_agent", "health"]

# What the container does: everything (Telegram buttons, sending, inbox, follow-ups, daily drafting), forever.
CMD ["python", "-m", "zypher_agent", "start"]
