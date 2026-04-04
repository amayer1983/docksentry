FROM python:3.12-alpine

LABEL maintainer="Andreas Mayer <andreas.mayer.1983@outlook.de>"
LABEL org.opencontainers.image.source="https://github.com/amayer1983/docksentry"
LABEL org.opencontainers.image.description="Docksentry — Docker container update manager with Telegram bot, Web UI, and auto-rollback"

RUN apk add --no-cache docker-cli docker-cli-compose

WORKDIR /app

COPY app/ .

RUN mkdir -p /data

ENV BOT_TOKEN=""
ENV CHAT_ID=""
ENV CRON_SCHEDULE="0 18 * * *"
ENV EXCLUDE_CONTAINERS=""
ENV AUTO_SELFUPDATE="false"
ENV LANGUAGE="en"
ENV WEB_UI="false"
ENV WEB_PORT=8080
ENV WEB_PASSWORD=""
ENV TZ="Europe/Berlin"
ENV PYTHONUNBUFFERED=1
ENV DOCKER_CONFIG=/.docker

VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD python3 /app/healthcheck.py || exit 1

ENTRYPOINT ["python3", "/app/main.py"]
