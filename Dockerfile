FROM python:3.12-alpine

# Injected at build time from the release tag (see docker-publish.yml);
# defaults to "dev" for local builds. Drives the OCI version label so
# Docksentry's own image reports a version in /status etc. (#39, @LeeNX).
ARG VERSION=dev

LABEL maintainer="Andreas Mayer <andreas.mayer.1983@outlook.de>"
LABEL org.opencontainers.image.title="Docksentry"
LABEL org.opencontainers.image.version="${VERSION}"
LABEL org.opencontainers.image.source="https://github.com/amayer1983/docksentry"
LABEL org.opencontainers.image.description="Docksentry — Docker container update manager with Telegram bot, Web UI, and auto-rollback"

RUN apk add --no-cache docker-cli docker-cli-compose

WORKDIR /app

COPY app/ .
# Read at startup to tell the user what the version they just pulled
# actually changed. Parsed rather than baked into a summary so the
# message and the file people read on GitHub cannot drift apart.
COPY CHANGELOG.md .

RUN mkdir -p /data

ENV BOT_TOKEN=""
ENV CHAT_ID=""
ENV CRON_SCHEDULE="0 18 * * *"
ENV EXCLUDE_CONTAINERS=""
ENV AUTO_SELFUPDATE="false"
ENV AUTO_CLEANUP="false"
ENV CLEANUP_GRACE_HOURS="24"
ENV CLEANUP_BACKUP_LOCAL_ONLY="false"
ENV CLEANUP_BACKUP_DAYS="7"
ENV DISK_WARN_PERCENT="85"
ENV DISK_WARN_AUTO_CLEANUP="false"
ENV QUIET_HOURS_START=""
ENV QUIET_HOURS_END=""
ENV WEEKLY_REPORT_ENABLED="false"
ENV WEEKLY_REPORT_WEEKDAY="0"
ENV WEEKLY_REPORT_HOUR="9"
ENV LANGUAGE="en"
ENV WEB_UI="false"
ENV WEB_PORT=8080
ENV WEB_PASSWORD=""
ENV DISCORD_WEBHOOK=""
ENV WEBHOOK_URL=""
ENV TZ="Europe/Berlin"
ENV PYTHONUNBUFFERED=1
ENV DOCKER_CONFIG=/.docker

VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD python3 /app/healthcheck.py || exit 1

ENTRYPOINT ["python3", "/app/main.py"]
