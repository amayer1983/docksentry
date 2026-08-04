"""Who did what, when, and through which front end.

The last open item on the v2.1 axis, and the only one that needed a data
model rather than an endpoint. Docksentry can be driven from four places —
the Web UI, Telegram, Discord and the scheduler — and until now none of
them left a trace that survived a restart. "Someone stopped the database
last night" was not an answerable question. The monitor's event log
answers what *happened to* containers; this answers what *people did to*
them.

Recorded at ONE seam per front end rather than at each action. There are
26 state-changing HTTP endpoints and 19 Discord commands today, and the
failure mode of instrumenting them one by one is not that it is tedious —
it is that number 27 gets added without a line and nobody notices, which
turns a complete record into a misleading one. A gap in an audit log is
worse than no audit log, because the absence of an entry reads as
evidence.

Secrets never reach the file. A settings save carries the Web UI password,
the bot token and the webhook URLs in its form body; writing those into a
plaintext log next to the config would hand an attacker who can read
`/data` everything at once. Redaction is a denylist applied to keys, and
it errs towards hiding: an unrecognised key whose *name* suggests a secret
is redacted rather than kept.
"""

import json
import os
import threading
from datetime import datetime

#: Key fragments whose values must never be written. Matched as substrings
#: against the lower-cased key, so `smtp_password`, `WEB_PASSWORD` and
#: `discord_webhook` are all caught by three entries.
SECRET_HINTS = ("password", "token", "secret", "webhook", "auth", "apikey",
                "api_key", "credential", "passwd", "bot_", "chat_id")

#: Values longer than this are cut. An audit line is a pointer to what
#: happened, not a copy of the payload.
MAX_VALUE = 120

#: Ring buffer size. At ~200 bytes an entry this is well under a megabyte,
#: and covers weeks of ordinary use.
MAX_ENTRIES = 500


def redact(params):
    """Form/command parameters with anything secret-looking replaced.

    Takes the parsed form dict (values may be lists, as `parse_qs`
    returns). Returns a flat {key: str} suitable for JSON.
    """
    out = {}
    for key, value in (params or {}).items():
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        text = str(value)
        low = str(key).lower()
        if any(h in low for h in SECRET_HINTS):
            # Length is safe to keep and occasionally useful ("they saved
            # an empty password") — the value is not.
            out[key] = f"<redacted:{len(text)}>" if text else "<empty>"
        elif len(text) > MAX_VALUE:
            out[key] = text[:MAX_VALUE] + "…"
        else:
            out[key] = text
    return out


class AuditLog:
    """Append-only record of state-changing actions, capped and atomic.

    Best-effort throughout: a failed write must never stop the action it
    was describing. An audit trail that can take the application down with
    it would be its own outage.
    """

    def __init__(self, config):
        self.path = getattr(config, "audit_file", "") or ""
        self._lock = threading.Lock()

    def record(self, source, actor, action, target="", detail=None):
        """Append one entry.

        source — "web", "telegram", "discord", "api", "schedule"
        actor  — user id, token name, or "system" for the scheduler
        action — the endpoint path or command name
        target — the container or group it acted on, when there is one
        detail — parameters; redacted here, not by the caller, so a new
                 call site cannot forget to do it
        """
        if not self.path:
            return
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
            "actor": str(actor or "?"),
            "action": action,
        }
        if target:
            entry["target"] = str(target)
        clean = redact(detail)
        if clean:
            entry["detail"] = clean
        try:
            from container_store import atomic_write_json
            with self._lock:
                entries = self._read()
                entries.append(entry)
                atomic_write_json(self.path, entries[-MAX_ENTRIES:])
        except Exception as e:                      # pragma: no cover
            print(f"Audit log error: {e}")

    def _read(self):
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (ValueError, OSError):
            return []

    def entries(self, limit=100):
        """Most recent first."""
        return list(reversed(self._read()[-limit:]))
