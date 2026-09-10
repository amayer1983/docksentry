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

# openssh-client: DOCKER_HOSTS documents ssh:// endpoints and the tests
# assert the argv for them, but the binary was never in the image — so
# every ssh:// host failed with `exec: "ssh": executable file not found`
# while the README said it worked. Measured, not inferred.
# tini: we run as PID 1, and PID 1 inherits every orphaned process in
# the container. The ssh masters that ControlPersist backgrounds are
# exactly that — the `docker` client exits, its ssh master is reparented
# to us, and Python never waits for a child it did not start. Measured on
# a live four-host install: 12074 defunct `ssh` processes, 9839 of them
# in a single hour, until the container could not fork at all and every
# host — including the tcp ones — reported "could not list containers".
#
# tini rather than a SIGCHLD handler of our own: a reaper calling
# waitpid(-1) races `subprocess.run()` for its children and can take an
# exit status out from under it. An update would then report success it
# never had, which is a far worse failure than the one being fixed.
RUN apk add --no-cache docker-cli docker-cli-compose openssh-client tini

# Reuse one ssh connection per host instead of building a new one for
# every command. Measured against an ssh:// host: a bare `ssh … true`
# costs 355 ms, so three quarters of every `docker -H ssh://…` call was
# the handshake — and the status page makes several per host. With
# multiplexing the same call goes from 475 ms to 148 ms, and the status
# page from 2.55 s to 1.32 s.
#
# In the image's own ssh_config, deliberately: the user's ~/.ssh is
# mounted in from their host and is theirs to keep. ControlPath lives in
# /tmp, which is ours, and the master closes after five idle minutes so a
# host that goes away does not leave a socket behind forever.
RUN printf '%s\n' \
      'Host *' \
      '    ControlMaster auto' \
      '    ControlPath /tmp/ds-ssh-%r@%h:%p' \
      '    ControlPersist 300' \
    >> /etc/ssh/ssh_config

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

VOLUME ["/docksentry"]

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD python3 /app/healthcheck.py || exit 1

ENTRYPOINT ["/sbin/tini", "--", "python3", "/app/main.py"]
