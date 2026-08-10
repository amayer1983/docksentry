#!/usr/bin/env python3
"""Password hashing and browser sessions for the Web UI (#60, @NotRetarded).

What was here before: HTTP Basic Auth against `WEB_PASSWORD`, with the
password kept **in plaintext** in `settings.json` and hashed per request
only to compare it. Three things followed from that, all of which he
listed and all of which turned out to be true when measured:

* the browser's Basic Auth dialog is not a page of this application, so a
  password manager has nothing to fill in;
* there was no username. The header was split into user and password and
  the user half was then never looked at again, so any username got in;
* the password sat in cleartext in the settings file and in the compose
  file, and the settings file's 0600 was the whole of the protection.

This module fixes the first two of those. It does not do TLS, and that is
deliberate rather than pending: anyone exposing this beyond their own
network puts a reverse proxy in front of it, and Caddy or Traefik does
certificates, including renewal, far better than we would.

**Hashing.** scrypt from the standard library, which is the whole reason
this is a small file — no dependency for something a project with none
should not be taking one for. Parameters are stored *in* the string, so
raising them later does not lock anyone out: an old hash still verifies
with the numbers it was made with.

**Compatibility is the constraint.** `WEB_PASSWORD` is an environment
variable and an environment variable is plaintext by nature; there is no
hashing that away. So `verify()` accepts a stored value in either form,
and a plaintext one is compared in constant time exactly as before. What
changes is what gets *written*: a password set through the interface is
stored hashed, and a plaintext one already in `settings.json` is migrated
on first start.
"""

import base64
import hashlib
import hmac
import os
import secrets
import time

#: Marker for our own format. Anything not starting with this is treated
#: as a plaintext password, which is what an env var necessarily is.
_PREFIX = "scrypt$"

#: scrypt cost parameters. n=16384 is roughly 100ms on this class of
#: hardware, which is the point: fast enough that a login does not feel
#: slow, slow enough that guessing at scale is unattractive. Stored in the
#: hash string, so these can be raised without invalidating old ones.
_N, _R, _P = 16384, 8, 1
_DKLEN = 32


def is_hashed(stored):
    """True if `stored` is one of our hashes rather than a plaintext one."""
    return isinstance(stored, str) and stored.startswith(_PREFIX)


def hash_password(password, salt=None):
    """`scrypt$n$r$p$salt$hash`, all base64, ready to put in settings.json."""
    salt = salt or os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P,
                        dklen=_DKLEN, maxmem=64 * 1024 * 1024)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"{_PREFIX}{_N}${_R}${_P}${b64(salt)}${b64(dk)}"


def verify(password, stored):
    """Does `password` match `stored`, in either form?

    Constant-time in both branches. The plaintext branch is not a
    fallback that should be removed later: `WEB_PASSWORD` in the
    environment can never be anything else.
    """
    if not stored:
        return False
    if not is_hashed(stored):
        return hmac.compare_digest(str(password), str(stored))
    try:
        _, n, r, p, salt_b64, hash_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r),
                            p=int(p), dklen=len(expected),
                            maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError, MemoryError):
        # A malformed hash must not let anybody in, and must not take the
        # process down either.
        return False
    return hmac.compare_digest(dk, expected)


def username_matches(submitted, configured):
    """Is this username acceptable?

    An unset `WEB_USERNAME` accepts any name, which is exactly what the
    old code did — by accident, having parsed the name and thrown it
    away. Kept on purpose now: every existing setup has a password and no
    username, and making the name suddenly matter would lock those people
    out of their own dashboards on upgrade.
    """
    want = (configured or "").strip()
    if not want:
        return True
    return hmac.compare_digest(str(submitted or "").strip(), want)


class SessionStore:
    """Browser sessions, in memory.

    In memory rather than on disk, and the interface says so: a restart
    signs everyone out. Writing them down would mean a file of live
    credentials on disk, which is most of what this change is trying to
    get away from, in exchange for not having to log in again after an
    update you performed yourself.

    Two clocks, because they answer different questions. **Idle** ends a
    session left open on a machine somebody walked away from. **Absolute**
    ends one that is being kept alive by a browser tab polling in the
    background, which no amount of idle timeout would ever catch.
    """

    def __init__(self, idle_seconds=8 * 3600, max_seconds=7 * 24 * 3600,
                 limit=200):
        self.idle = int(idle_seconds)
        self.max = int(max_seconds)
        #: Bound, so a script hammering the login form cannot grow this
        #: without limit. Oldest first out.
        self.limit = int(limit)
        self._sessions = {}

    def _now(self):
        return time.time()

    def create(self, username):
        """A new session token. 32 bytes from `secrets`, so it is not
        guessable and does not encode anything about the user."""
        self.sweep()
        if len(self._sessions) >= self.limit:
            oldest = min(self._sessions, key=lambda k: self._sessions[k][1])
            self._sessions.pop(oldest, None)
        token = secrets.token_urlsafe(32)
        now = self._now()
        self._sessions[token] = (username or "", now, now)
        return token

    def validate(self, token):
        """The username for a live session, or None. Touches it on success
        so that using the interface keeps it alive."""
        if not token:
            return None
        entry = self._sessions.get(token)
        if not entry:
            return None
        username, created, seen = entry
        now = self._now()
        if now - created > self.max or now - seen > self.idle:
            self._sessions.pop(token, None)
            return None
        self._sessions[token] = (username, created, now)
        return username

    def destroy(self, token):
        self._sessions.pop(token or "", None)

    def clear(self):
        """Every session, gone. What a password change has to do: the old
        password's sessions must not outlive it."""
        self._sessions.clear()

    def sweep(self):
        now = self._now()
        for token, (_, created, seen) in list(self._sessions.items()):
            if now - created > self.max or now - seen > self.idle:
                self._sessions.pop(token, None)

    def __len__(self):
        return len(self._sessions)
