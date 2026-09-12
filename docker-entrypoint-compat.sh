#!/bin/sh
# This is NOT tini, and it lives at /sbin/tini on purpose.
#
# 2.17.10 and 2.18.0-beta.27 shipped an init as ENTRYPOINT to stop the
# ssh process leak. Their self-update reapplies the running container's
# entrypoint path onto the new image and drops the arguments that
# follow it, so a container created by those two versions asks the next
# image for `/sbin/tini` with nothing after it. Without this file that
# recreate fails outright, and the only way forward is by hand — which
# a user who never reads the issue tracker will never know to do.
#
# With no arguments it starts Docksentry, which is what that broken
# recreate is really asking for. With arguments it runs them, so an
# entrypoint of `/sbin/tini -- python3 /app/main.py` also lands right.
#
# It stays. When a real init comes back it goes somewhere else, because
# the real tini exits 1 when called with no arguments, and every
# container still created by 2.17.10 would be back in a restart loop.
[ "$1" = "--" ] && shift
[ $# -eq 0 ] && exec python3 /app/main.py
case "$1" in
  -*)
    # Somebody is using this as if it were tini. Say so rather than
    # passing the flag to `exec` and failing with "illegal option".
    echo "/sbin/tini in this image is a compatibility shim, not tini." >&2
    echo "It takes a command to run, or nothing at all." >&2
    exit 64
    ;;
esac
exec "$@"
