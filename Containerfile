# Proto-Sync — FreeFileSync-style compare & sync web app, rsync engine.
# Build:  podman build -t proto-sync .      (docker build works the same)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PROTOSYNC_DATA=/data \
    PROTOSYNC_PORT=8475 \
    TZ=UTC

# rsync   : the transfer engine
# tini    : proper PID 1 so rsync children are reaped and SIGTERM reaches uvicorn
# tzdata  : schedules fire in your local time (set TZ)
RUN apt-get update \
 && apt-get install -y --no-install-recommends rsync tini tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/proto-sync
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY static ./static

VOLUME ["/data"]
EXPOSE 8475

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PROTOSYNC_PORT\",\"8475\")}/api/health', timeout=4)" || exit 1

# Runs as root *inside* the container. Under rootless Podman that maps to your
# own user on the host — exactly the permissions media-backup.sh had.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]
