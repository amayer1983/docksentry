#!/usr/bin/env python3
"""Tapping the same update twice, and what the second tap is told.

Seen on the local instance on 08.10.2026, in this order:

    ⏳ Update `portainer`...
    ✅ portainer @srv30: OK (2026-09-17 → 2026-10-08, 43 MB)
    ✅ Alle Updates abgeschlossen.
    ⚠️ Container `portainer` nicht in der Update-Liste.

A warning under a green result, for work that had just succeeded. The
update was fine; the message was not.

What happened: a second tap on a container whose update is already
running cannot take the lock, so it was put in the queue. The first
update then finished, removed the entry from the pending list and said
so — and the drain picked the queued duplicate up, found no pending
entry for it, and reported the one message that fits a typo rather than
a double tap.

Two halves, and both are needed:

* a second tap on the SAME container is not a second piece of work. It
  is told so and not queued. For that the single-update path has to say
  what it is doing, which it never did — only the batch path marked a
  container as in flight, so the Web UI also showed no badge for an
  update started from a chat.
* if one does end up queued anyway — a different route, a restart, a
  race — the drain says "already updated while this waited", not "not
  in the update list". The second is still right for a name somebody
  typed that is not pending, so both messages exist.

And every one of them names the host. `portainer` alone is ambiguous
the moment two machines run one, which is the ordinary case for a
reverse proxy, portainer or watchtower — and it was the ambiguous form
that appeared under a result line that did name the host.
"""
import json
import os
import sys
import tempfile
import threading
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from i18n import get_translator  # noqa: E402
from telegram_bot import TelegramBot  # noqa: E402
from update_engine import UpdateEngine  # noqa: E402

checks = {}


def _bot(pending_entries, on_update=None):
    """A bot wired to a real UpdateEngine and a pending file on disk."""
    tmp = tempfile.mkdtemp(prefix="dup-tap-")
    pending = os.path.join(tmp, "pending.json")
    with open(pending, "w", encoding="utf-8") as f:
        json.dump(pending_entries, f)

    said = []
    bot = TelegramBot.__new__(TelegramBot)

    class _Cfg:
        pending_file = pending
        language = "en"

        def __getattr__(self, name):
            return ""

    bot.config = _Cfg()
    bot.t = get_translator("en")
    bot.send_message = lambda m, **kw: said.append(m)
    bot.log = lambda *a, **kw: None
    bot._enrich_with_source_url = lambda ups: None
    bot._display_name = lambda u: f"{u['name']} @{u.get('host', 'local')}"
    bot._host_by_name = lambda n: types.SimpleNamespace(name=n)
    bot._checker_for = lambda host, checker: checker
    bot._pending_key = lambda u: (u.get("host") or "local", u["name"])
    bot._group_of = lambda k: None
    bot._run_queued_selfupdate = lambda: None

    # A real engine: the lock, the queue and the in-flight map are what
    # the two halves above are about, so none of them is faked.
    eng = UpdateEngine.__new__(UpdateEngine)
    eng._update_lock = threading.Lock()
    eng._update_queue = []
    eng._update_queue_lock = threading.Lock()
    eng._updating = {}
    eng._queued_selfupdate = None
    eng._swap_in_flight = False
    bot.engine = eng
    bot.notifier = None

    class _Checker:
        """Runs `on_update` ONCE, during the first update.

        Once, because it stands for a person tapping a button while one
        update is in flight — and a hook that fires on every update has
        the drained entry tap itself, which is the fixture talking, not
        the program.
        """

        fired = False

        def update_container(self, name, image, **kw):
            if on_update is not None and not _Checker.fired:
                _Checker.fired = True
                on_update(bot)
            return True, "OK"

    return bot, _Checker(), said


ENTRY = [{"name": "portainer", "image": "portainer/portainer-ce:latest",
          "host": "srv30"}]
KEY = "srv30/portainer"


# ── the double tap ───────────────────────────────────────────────────
def _tap_again(b):
    b._run_single_update(_tap_again.checker, KEY)


bot, checker, said = _bot(ENTRY, on_update=_tap_again)
_tap_again.checker = checker
bot._run_single_update(checker, KEY)

_text = "\n".join(said)
checks["a second tap during the update is told it is the same one"] = (
    "being updated right now" in _text)
checks["…and is not left in the queue afterwards"] = (
    bot.engine._update_queue == [])
# The line that started this: a warning under a green result.
checks["…so nothing claims the container is not in the update list"] = (
    "not in update list" not in _text)
checks["…and the update itself still finished"] = (
    "All updates" in _text or "complete" in _text.lower())
# Every message names the host, because `portainer` alone is ambiguous
# the moment two machines run one.
# Read off the SECOND TAP's own line, not the whole transcript. The
# result line "✅ portainer @srv30: OK" names the host too, so looking
# for it anywhere satisfied this check even with the host taken out of
# the message under test — measured.
_tap_line = next((m for m in said if "being updated right now" in m), "")
checks["…and the second tap's answer names the host"] = (
    "portainer @srv30" in _tap_line)
# …and so do the lines around it, which is what "every one of them"
# claimed and three of them did not do.
checks["…as does the line that confirms the tap"] = any(
    "portainer @srv30" in m for m in said if "Updating" in m)

# ── the in-flight marker the single path never set ───────────────────
# Without it the half above cannot work at all, and the Web UI showed
# no "updating" badge for a container updated from a chat.
seen = {}
bot2, checker2, _ = _bot(ENTRY,
                         on_update=lambda b: seen.update(b.engine.updating))
bot2._run_single_update(checker2, KEY)
checks["the single-update path says what it is updating"] = KEY in seen
checks["…and stops saying it once the update is done"] = (
    bot2.engine.updating == {})

# ── a queued duplicate that got through anyway ───────────────────────
# A different route, a restart, a race: the drain must not call finished
# work a mistake.
bot3, checker3, said3 = _bot([])          # nothing pending any more
bot3._run_single_update(checker3, KEY, from_queue=True)
_t3 = "\n".join(said3)
checks["a queued entry whose work is done says exactly that"] = (
    "already updated" in _t3 and "not in update list" not in _t3)
checks["…naming the host it belonged to"] = "portainer @srv30" in _t3

# ── …but a name that really is not pending still says so ─────────────
# The old message is right for this case, which is why both exist.
bot4, checker4, said4 = _bot(ENTRY)
bot4._run_single_update(checker4, "srv30/nosuchcontainer")
checks["a name that is not pending is still 'not in update list'"] = (
    "not in update list" in "\n".join(said4))

# ── a DIFFERENT container tapped during an update is still queued ────
# The guard must not swallow real work: two taps on two containers are
# two jobs, and dropping the second is the bug the queue was added for.
def _tap_other(b):
    b._run_single_update(_tap_other.checker, "srv30/other")


TWO = ENTRY + [{"name": "other", "image": "other:1", "host": "srv30"}]
bot5, checker5, said5 = _bot(TWO, on_update=_tap_other)
_tap_other.checker = checker5
bot5._run_single_update(checker5, KEY)
_t5 = "\n".join(said5)
checks["a different container tapped meanwhile is still queued and run"] = (
    "queued" in _t5.lower() or "other" in _t5)
checks["…and is not told it is the same one"] = (
    "being updated right now" not in _t5)
checks["…and really does run, rather than being dropped"] = (
    "Updating `other" in _t5)
if "being updated right now" in _t5:
    print("      → " + _t5.replace("\n", " | ")[:220])

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
