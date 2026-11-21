#!/usr/bin/env python3
"""Telegram callback matrix: what every button press does today, through the real `_handle_callback`.

The command matrix (test_telegram_dispatch_matrix.py) pins what a typed
command answers. A button press takes a second entry path, and the core
split moves what sits behind it too: the pending file gets one reader
(Q5), the queue drain gets its own checker (Q7b), a host that left the
registry is refused (Q11b), "Update all" splits a mixed batch by host
(Q12), and the already-used button gets an answer (D10). This drives
every `callback_data` family the bot hands out, plus `noop`, against a
real `TelegramBot` and compares the result with a table taken from main
before anything moved.

Same three set-ups and the same fixture as the command matrix (imported
from it, not copied): `multi` (local, nas, dead, refused, flaky), `none`
(no registry) and `one` (a registry holding only the local host).

A row is a short scenario, because most presses only mean something
after the message that offered the button: `/stop web` and then
`stop_go:<tok>`, a notification and then `update_one:nas/web`. Each
step is recorded on its own: the transport log (reply texts, toasts,
button removals and keyboard edits, each keyboard as label→data),
which host backends were called and with what, the environment events
(which checker `update_container` ran on, among others), and the
exception the step raised. Threads a step starts are joined before the
next step, and an exception inside one lands in the step's events
through `threading.excepthook`. After the last step the row records the
pending file, the update queue, the live stop and restore offers and
the deferred majors per host.

The one-time tokens of the stop and restore offers are random; they are
read off the offered keyboard, pressed as they are, and written as
`<tok>` in the record. Snapshot tokens are a counter and stay as they
are.

Stand-ins, on top of the command matrix's (its header lists them):

  * `bot._download_file` (Telegram's file download) returns a small
    backup bundle, so an uploaded `.json` goes through the real
    `_offer_restore`;
  * the checker's `_wait_healthy`, which `restart_deps:` calls directly
    and which polls every 10 s for up to 30 s; it records and answers
    "healthy";
  * the engine's `_sleep` and `_clock`: a cooldown or a group's pause
    is recorded as a `wait Ns` event instead of sat out, on a clock
    that stands still (Q11c);
  * holding the update lock from outside (`lock` / `unlock` steps)
    stands for another update running;
  * an `age` step moves the live stop and restore offers past their
    TTL by rewriting their `created` stamp, instead of waiting 15
    minutes.

`python3 scripts/test_telegram_callback_matrix.py --snapshot [--app DIR]`
prints a fresh expectation table from DIR's app code (default this
tree's). The committed table was generated from main (8a74ba7) and then
read row by row against the branch each press lands in.
"""

import contextlib
import io
import json
import os
import pprint
import re
import shutil
import sys
import tempfile
import time

import test_telegram_dispatch_matrix as m

CHAT, USER, OTHER = m.CHAT, m.USER, "8"

# ── the scenarios ──────────────────────────────────────────────────────
_CUT = "cut off mid-write"
#: Steps, in order:
#:   ("type", text[, user])     a typed message through _handle_message
#:   ("upload", user)           a backup .json dropped into the chat
#:   ("notify", hosts)          the real notify_updates over the pending
#:                              entries of those hosts (None = all)
#:   ("tap", data[, user[, chat]])  a press; "<tok>" in data is the token
#:                              of the newest offered button that fits
#:   ("lock",) / ("unlock",)    hold / release the shared update lock
#:   ("release",)               give it back through the engine's door,
#:                              which starts the drainer (Q16)
#:   ("fill", n)                put `q0` … `q<n-1>` on the queue
#:   ("mark_updating", key)     the engine counts `key` as being updated
#:   ("age",)                   push live stop/restore offers past the TTL
#:   ("major", host, name)      a deferred major in that host's store
#:   ("group", host, gid, members)  a group with restart_dependents on
#:                              in that host's store
#:   ("cooldown", host, name, seconds)  an update cooldown in that
#:                              host's store
#:   ("add_pending", name, image, host)
#:   ("drop_pending",)          remove every pending entry (the real
#:                              _remove_from_pending)
#:   ("raw_pending", bytes)     overwrite the pending file with these bytes
#:                              (Q6: a file Docksentry cannot read)
SCENARIOS = [
    ("unauthorised chat", [("tap", "update_all", USER, "999")]),
    # ── stop ────────────────────────────────────────────────────────
    ("stop_cancel", [("type", "/stop web"), ("tap", "stop_cancel"),
                     ("tap", "stop_go:<tok>")]),
    ("stop_go owner", [("type", "/stop web"), ("tap", "stop_go:<tok>"),
                       ("tap", "stop_go:<tok>")]),
    ("stop_go @nas", [("type", "/stop web @nas"),
                      ("tap", "stop_go:<tok>")]),
    ("stop_go foreign", [("type", "/stop web"),
                         ("tap", "stop_go:<tok>", OTHER),
                         ("tap", "stop_go:<tok>")]),
    ("stop_go expired", [("type", "/stop web"), ("age",),
                         ("tap", "stop_go:<tok>")]),
    ("stop_go unknown token", [("tap", "stop_go:0123456789ab")]),
    # ── restore ─────────────────────────────────────────────────────
    ("restore_cancel:<tok>", [("upload", USER), ("tap", "restore_cancel:<tok>"),
                              ("tap", "restore_go:<tok>")]),
    ("restore_cancel bare", [("upload", USER), ("tap", "restore_cancel"),
                             ("tap", "restore_go:<tok>")]),
    ("restore_go owner", [("upload", USER), ("tap", "restore_go:<tok>"),
                          ("tap", "restore_go:<tok>")]),
    ("restore_go foreign", [("upload", USER),
                            ("tap", "restore_go:<tok>", OTHER),
                            ("tap", "restore_go:<tok>")]),
    ("restore_go expired", [("upload", USER), ("age",),
                            ("tap", "restore_go:<tok>")]),
    ("restart_self", [("tap", "restart_self")]),
    # ── update all / skip ───────────────────────────────────────────
    ("update_all bare", [("tap", "update_all")]),
    ("update_all:<tok> local", [("notify", ["local"]),
                                ("tap", "update_all:<tok>")]),
    ("update_all:<tok> mixed", [("notify", None),
                                ("tap", "update_all:<tok>")]),
    ("update_all:<tok> mixed, nas group head",
     [("group", "nas", "g1", ["web", "media"]), ("notify", None),
      ("tap", "update_all:<tok>")]),
    ("update_all:<tok> mixed, dead host",
     [("add_pending", "web", "nginx:1.25", "dead"), ("notify", None),
      ("tap", "update_all:<tok>")]),
    ("update_all:<tok> mixed, gone host",
     [("add_pending", "web", "nginx:1.25", "gone"), ("notify", None),
      ("tap", "update_all:<tok>")]),
    ("update_all:<tok> stale", [("tap", "update_all:7")]),
    ("update_all lock held", [("notify", None), ("lock",),
                              ("tap", "update_all:<tok>"), ("unlock",)]),
    # Q16: busy, the snapshot goes on the queue and the drainer runs it
    # once the lock comes back through the door.
    ("update_all lock held, then released",
     [("notify", None), ("lock",), ("tap", "update_all:<tok>"),
      ("release",)]),
    ("update_all bare lock held", [("lock",), ("tap", "update_all"),
                                   ("unlock",)]),
    ("update_all lock held, web being updated",
     [("notify", None), ("lock",), ("mark_updating", "web"),
      ("tap", "update_all:<tok>"), ("unlock",)]),
    ("update_skip", [("notify", None), ("tap", "update_skip")]),
    # ── update one ──────────────────────────────────────────────────
    ("update_one:web", [("notify", None), ("tap", "update_one:web")]),
    ("update_one:nas/web", [("notify", None), ("tap", "update_one:nas/web")]),
    ("update_one not pending", [("tap", "update_one:db")]),
    ("update_one gone host", [("add_pending", "web", "nginx:1.25", "gone"),
                              ("tap", "update_one:gone/web")]),
    ("update_one empty file", [("drop_pending",), ("tap", "update_one:web")]),
    # The file a single update leaves when it took the last entry is `[]`,
    # not gone; a tap on it says "not in the list", not "nothing pending".
    ("update_one after the last single update",
     [("notify", None), ("tap", "update_one:web"), ("tap", "update_one:web")]),
    # ── Q6: a pending file Docksentry cannot read ───────────────────
    ("update_one unreadable file", [("raw_pending", m.Q6_FILES[_CUT]),
                                    ("tap", "update_one:web")]),
    ("update_all bare unreadable file", [("raw_pending", m.Q6_FILES[_CUT]),
                                         ("tap", "update_all")]),
    ("update_all:<tok> unreadable file",
     [("notify", None), ("raw_pending", m.Q6_FILES[_CUT]),
      ("tap", "update_all:<tok>")]),
    ("update_skip unreadable file",
     [("notify", None), ("raw_pending", m.Q6_FILES[_CUT]),
      ("tap", "update_skip")]),
    ("queue drain after a remote tap",
     [("add_pending", "db", "postgres:16", "local"), ("lock",),
      ("tap", "update_one:db"), ("unlock",), ("tap", "update_one:nas/web")]),
    ("queue drain after a local tap",
     [("add_pending", "db", "postgres:16", "local"), ("lock",),
      ("tap", "update_one:nas/web"), ("unlock",), ("tap", "update_one:db")]),
    ("queue twice while locked",
     [("add_pending", "db", "postgres:16", "local"), ("lock",),
      ("tap", "update_one:db"), ("tap", "update_one:db"), ("unlock",)]),
    # Q11c: a tap runs through the shared batch. The cooldown of the
    # container just updated holds the queued one behind it, the queued
    # group-mate gets the group's wait, and the head's dependents are
    # restarted — as "Update all" does with the same containers.
    ("queue drain after a cooldown container",
     [("cooldown", "local", "web", 60),
      ("add_pending", "db", "postgres:16", "local"), ("lock",),
      ("tap", "update_one:db"), ("unlock",), ("tap", "update_one:web")]),
    ("update_one cooldown container, nothing queued",
     [("cooldown", "local", "web", 60), ("notify", None),
      ("tap", "update_one:web")]),
    ("queue drain after a group head",
     [("group", "local", "g1", ["web", "db"]),
      ("add_pending", "db", "postgres:16", "local"), ("lock",),
      ("tap", "update_one:db"), ("unlock",), ("tap", "update_one:web")]),
    # ── majors ──────────────────────────────────────────────────────
    ("confirm_major:web", [("major", "local", "web"),
                           ("tap", "confirm_major:web")]),
    ("confirm_major:nas/web", [("major", "nas", "web"),
                               ("tap", "confirm_major:nas/web")]),
    ("confirm_major gone host", [("major", "gone", "web"),
                                 ("tap", "confirm_major:gone/web")]),
    ("confirm_major gone host, nothing deferred",
     [("tap", "confirm_major:gone/web")]),
    ("confirm_major none pending", [("tap", "confirm_major:web")]),
    ("confirm_major lock held", [("major", "local", "web"), ("lock",),
                                 ("tap", "confirm_major:web"), ("unlock",)]),
    ("confirm_major lock held, confirmed again from its answer",
     [("major", "local", "web"), ("lock",), ("tap", "confirm_major:web"),
      ("unlock",), ("tap", "confirm_major:web")]),
    ("confirm_major:nas/web lock held",
     [("major", "nas", "web"), ("lock",), ("tap", "confirm_major:nas/web"),
      ("unlock",)]),
    # ── typed updates with the lock held (Q16) ──────────────────────
    ("/update web lock held", [("lock",), ("type", "/update web"),
                               ("unlock",)]),
    ("/update web* lock held, then released",
     [("lock",), ("type", "/update web*"), ("release",)]),
    ("/update db lock held", [("lock",), ("type", "/update db"),
                              ("unlock",)]),
    ("/update web @all lock held", [("lock",), ("type", "/update web @all"),
                                    ("unlock",)]),
    ("/updateall lock held", [("lock",), ("type", "/updateall"),
                              ("unlock",)]),
    ("/updateall @nas lock held", [("lock",), ("type", "/updateall @nas"),
                                   ("unlock",)]),
    ("/updateall @all lock held, then released",
     [("lock",), ("type", "/updateall @all"), ("release",)]),
    ("reject_major:web", [("major", "local", "web"),
                          ("tap", "reject_major:web")]),
    ("reject_major:nas/web", [("major", "nas", "web"),
                              ("tap", "reject_major:nas/web")]),
    ("reject_major gone host", [("major", "local", "web"),
                                ("tap", "reject_major:gone/web")]),
    # ── lifecycle / groups / leftovers ──────────────────────────────
    ("lifecycle:stop:web", [("tap", "lifecycle:stop:web")]),
    ("lifecycle:restart:web", [("tap", "lifecycle:restart:web")]),
    ("lifecycle:bad:web", [("tap", "lifecycle:bad:web")]),
    ("lifecycle:stop", [("tap", "lifecycle:stop")]),
    ("restart_deps:<gid>", [("group", "local", "g1", ["web", "db"]),
                            ("tap", "restart_deps:g1")]),
    ("restart_deps one member", [("group", "local", "g1", ["web"]),
                                 ("tap", "restart_deps:g1")]),
    ("restart_deps unknown", [("tap", "restart_deps:nosuch")]),
    ("noop", [("notify", None), ("tap", "update_one:web"), ("tap", "noop")]),
    ("unknown data", [("tap", "bogus")]),
]

#: Rows that are wrong today and pinned as they are, with the slice
#: meant to change them. Read off the generated table by hand.
KNOWN_DEFECTS = {
    ("multi", "noop"): "D10: the used button is never answered",
    ("none", "noop"): "D10: the used button is never answered",
    ("one", "noop"): "D10: the used button is never answered",
}

#: Rows another slice leans on as they are today (not wrong, but named).
PINNED_FOR = {
    ("multi", "update_one empty file"):
        "Q5: the emptied pending file is deleted, the tap says nothing pending",
    ("none", "update_one empty file"):
        "Q5: the emptied pending file is deleted, the tap says nothing pending",
    ("one", "update_one empty file"):
        "Q5: the emptied pending file is deleted, the tap says nothing pending",
}

#: Where each family lands, read against main at 8a74ba7
#: (app/telegram_bot.py, `_handle_callback`). Printed above its rows by
#: --snapshot, so a regenerated table keeps the review notes.
BRANCH_NOTES = {
    "unauthorised chat": ":3262 `_check_auth` refuses before the audit "
        "seam and answers the toast only.",
    "stop_cancel": ":3288 clears every live stop offer, so the token is "
        "gone for the next press.",
    "stop_go": ":3295 buttons off first, then the owner gate (fail closed), "
        "the TTL, the pop; the press re-runs `lifecycle.act` with the "
        "offer's own @token.",
    "restore_cancel": ":3343 drops only its own token; the bare legacy "
        "form drops nothing, so the offer still restores.",
    "restore_go": ":3362 owner gate before the buttons come off; the "
        "sweep at :3256 has already dropped an expired offer.",
    "restart_self": ":3416 `restart_self` with the caller's checker.",
    "update_all": ":3423 token → snapshot (stale answers and stops), bare "
        "→ the whole pending file; `run_updates` on a thread with the "
        "caller's (local) checker. Since Q12 it runs one batch per host, "
        "in the order the hosts first appear, each with that host's "
        "checker: the netns snapshot (`inspect --format`) and a group "
        "head's cascade (wait_healthy, the dependents' restart) go to the "
        "entry's own host. A host that stops answering fails its own entry "
        "from the snapshot; a host the registry does not know keeps the "
        "caller's checker and the batch refuses it. Since Q16 a busy lock "
        "queues the whole set (snapshot, or the file for the bare form) "
        "through `claim_or_queue_many` instead of 'already in progress'; "
        "one key gets the tap's sentence, several a heading (naming what "
        "is being updated, if anything) and a line each. Released through "
        "the door, the drainer runs them.",
    "update_skip": ":3451 multi keeps the other hosts' entries, single "
        "deletes the file.",
    "update_one": ":3479 rebuilds the keyboard without the tapped button "
        "(`noop`), `_run_single_update` on a thread; it reassigns "
        "`checker` to the tapped key's host (:1798). Until Q7b it handed "
        "that one to the drain (:1929), and `_checker_for` returns the "
        "caller's checker for the local host, so a queued local key ran on "
        "nas's; now the drain gets the checker the tap was called with. "
        "A pending key whose host the registry does not know (`gone`) "
        "is refused in `run_keys` since Q11b: 'Unknown host', nothing "
        "runs, the entry stays. Since Q11c `run_keys` runs each key "
        "through the shared batch: the netns snapshot asks the tapped "
        "key's own host (the extra `inspect --format`), a group head's "
        "dependents are restarted, and a queued key waits the last "
        "recreate's cooldown and its group's pause first.",
    "queue": ":1825 lock busy → enqueue (position, then 'already in the "
        "queue'). Since Q13 the engine's drainer works it off once the "
        "tap that held the lock releases it, whatever that tap did: one "
        "that ran nothing (key not listed, so on a single host the remote "
        "tap) used to return before the drain, and the queued `db` waited "
        "for the next sweep. Now it runs right after.",
    "confirm_major": ":3497 on a thread, `_confirm_major_update`; busy "
        "lock still refuses and does not queue (my decision for Q16), with "
        "a sentence that says so and that the major still waits. A key "
        "whose host the registry does not know (`gone`, and `nas` on a "
        "single host) answers 'Unknown host' since Q11b, before anything "
        "is looked up; nothing runs and the deferred major stays.",
    "reject_major": ":3508 drops the deferred major in that host's store. "
        "A key whose host the registry does not know (`gone`, and `nas` "
        "on a single host) answers 'Unknown host' since Q11b, like "
        "confirm_major: before, `_store_for` handed it the local store, "
        "so the local `web`'s deferred major went and the chat said "
        "'skipped'.",
    "lifecycle": ":3520 local only; a bad split or action is a toast.",
    "/update": "typed, with the lock held (Q16): no lookup on a host and "
        "no `check_all`; the pending entries the pattern names (exactly, "
        "or as a glob) on the targeted hosts go to `run_updates` on a "
        "thread, which queues them. A name the pending list does not hold "
        "answers 'already in progress' as before. Without `@host` it "
        "stays local, so nas's `web` is still pending after the release.",
    "/updateall": "typed, with the lock held (Q16): the same set as the "
        "free path (the file, or its `@host` part) goes through "
        "`_handle_callback` to `run_updates` and is queued. Without "
        "`@host` it is the local host on multi as well.",
    "restart_deps": ":3548 local group, needs two members; the cascade "
        "runs on a thread.",
    "noop": "no branch: nothing answers.",
    "unknown data": "no branch: nothing answers.",
}


def _family(name):
    for key in sorted(BRANCH_NOTES, key=len, reverse=True):
        if name.startswith(key):
            return key
    if name.startswith("queue"):
        return "queue"
    return name


# ── the restore bundle a dropped file holds ────────────────────────────
def _bundle():
    import backup
    return {"schema_version": backup.SCHEMA_VERSION, "instance": "matrix",
            "generated_at": "2026-11-19T10:00:00", "docksentry_version": "x",
            "pinned": ["db"]}


# ── one row ────────────────────────────────────────────────────────────
def _kb_str(markup):
    rows = (markup or {}).get("inline_keyboard") or []
    if not rows:
        return ""
    return " ".join("[" + " | ".join(
        f"{b.get('text', '')}→{b.get('callback_data', '')}" for b in r) + "]"
        for r in rows)


class Row:
    def __init__(self, setup):
        self.setup = setup
        (self.bot, self.checker, self.backends, self.log,
         self.data) = m.build(setup)
        self.markups = []           # every reply_markup sent, in order
        self.tokens = {}            # real token → "<tok>"
        bot, log = self.bot, self.log

        def send_message(text, reply_markup=None, auto=False, _held=False):
            log.append(("send", text, _kb_str(reply_markup)))
            self.markups.append(reply_markup)
            for r in (reply_markup or {}).get("inline_keyboard") or []:
                for b in r:
                    mt = re.match(r"(stop_go|restore_go|restore_cancel):"
                                  r"([0-9a-f]{12})$",
                                  b.get("callback_data") or "")
                    if mt:
                        self.tokens[mt.group(2)] = "<tok>"
            return True

        def api_call(method, data=None, *a, **k):
            kb = ""
            if data and "reply_markup" in data:
                kb = _kb_str(json.loads(data["reply_markup"])) or "[]"
            log.append(("api", method, kb))
            return {"ok": True, "result": {}}

        bot.send_message = send_message
        bot.api_call = api_call
        bot._download_file = lambda file_id: (
            m.EVENTS.append(f"download_file({file_id!r})"),
            json.dumps(_bundle()).encode())[1]

        def wait_healthy(ch, name, max_starting=None, interval=10):
            m.EVENTS.append(f"wait_healthy@{ch.backend.name}:{name} "
                            f"max={max_starting}")
            return "healthy", "running", "healthy"
        type(self.checker)._wait_healthy = wait_healthy
        # The engine's waits (a cooldown, a group's pause) are recorded
        # instead of sat out, on a clock that stands still.
        bot.engine._sleep = lambda s: m.EVENTS.append(f"wait {s:g}s")
        bot.engine._clock = lambda: 1000.0
        self.n = 0

    # ── steps ──
    def _pending(self):
        from telegram_bot import read_pending
        return read_pending(self.bot.config.pending_file)

    def _resolve(self, data):
        if "<tok>" not in data:
            return data, None
        pat = re.compile("^" + re.escape(data).replace(
            re.escape("<tok>"), "([0-9a-f]+)") + "$")
        for i in range(len(self.markups) - 1, -1, -1):
            for r in (self.markups[i] or {}).get("inline_keyboard") or []:
                for b in r:
                    if pat.match(b.get("callback_data") or ""):
                        return b["callback_data"], i
        return data.replace("<tok>", "none-offered"), None

    def _markup_holding(self, data):
        for i in range(len(self.markups) - 1, -1, -1):
            for r in (self.markups[i] or {}).get("inline_keyboard") or []:
                if any(b.get("callback_data") == data for b in r):
                    return i
        return None

    def do(self, step):
        kind, args = step[0], step[1:]
        bot = self.bot
        if kind == "type":
            user = args[1] if len(args) > 1 else USER
            bot._handle_message({"message_id": 1, "text": args[0],
                                 "from": {"id": int(user)},
                                 "chat": {"id": int(CHAT)}},
                                self.checker, None)
        elif kind == "upload":
            bot._handle_message({"message_id": 1, "text": "",
                                 "document": {"file_name": "backup.json",
                                              "file_size": 200,
                                              "file_id": "F1"},
                                 "from": {"id": int(args[0])},
                                 "chat": {"id": int(CHAT)}},
                                self.checker, None)
        elif kind == "notify":
            from telegram_bot import pending_host
            rows = [u for u in self._pending()
                    if args[0] is None or pending_host(u) in args[0]]
            bot.notify_updates(rows)
        elif kind == "tap":
            data, idx = self._resolve(args[0])
            if idx is None:
                idx = self._markup_holding(data)
            user = args[1] if len(args) > 1 else USER
            chat = args[2] if len(args) > 2 else CHAT
            self.n += 1
            msg = {"message_id": 100 + (idx if idx is not None else 0),
                   "chat": {"id": int(chat)}}
            if idx is not None and self.markups[idx]:
                msg["reply_markup"] = self.markups[idx]
            bot._handle_callback({"id": f"cb{self.n}", "data": data,
                                  "from": {"id": int(user)},
                                  "message": msg}, self.checker)
        elif kind == "lock":
            bot._update_lock.acquire()
        elif kind == "unlock":
            bot._update_lock.release()
        elif kind == "release":
            bot.engine.release_update()
        elif kind == "fill":
            bot.engine.enqueue_updates([f"q{i}" for i in range(args[0])])
        elif kind == "mark_updating":
            bot.engine.mark_updating(args[0])
        elif kind == "age":
            for table in ("_pending_stops", "_pending_restores"):
                for rec in (getattr(bot, table, None) or {}).values():
                    rec["created"] = time.time() - bot.STOP_CONFIRM_TTL - 1
        elif kind == "major":
            bot._store_for(args[0]).add_pending_major(args[1], {
                "image": "nginx:1.25", "old_version": "1.25.0",
                "new_version": "2.0.0", "compose": {}})
        elif kind == "group":
            bot._store_for(args[0]).save_group(args[1], args[1], args[2],
                                               wait_seconds=5,
                                               restart_dependents=True)
        elif kind == "cooldown":
            bot._store_for(args[0]).set_cooldown(args[1], args[2])
        elif kind == "add_pending":
            from container_store import atomic_write_json
            rows = self._pending() + [{
                "name": args[0], "image": args[1], "host": args[2],
                "old_version": "1.0.0", "new_version": "1.1.0"}]
            atomic_write_json(bot.config.pending_file, rows)
        elif kind == "drop_pending":
            from telegram_bot import pending_key
            bot._remove_from_pending([pending_key(u)
                                      for u in self._pending()])
        elif kind == "raw_pending":
            with open(bot.config.pending_file, "wb") as f:
                f.write(args[0])
        else:
            raise ValueError(f"unknown step {kind!r}")

    def norm(self, s):
        from version import VERSION
        s = str(s).replace(self.data, "{DATA}").replace(VERSION, "{VERSION}")
        for tok, rep in self.tokens.items():
            s = s.replace(tok, rep)
        return m._short(s)

    def label(self, step):
        parts = [step[0]] + [
            x if isinstance(x, str) else repr(x) for x in step[1:]]
        return self.norm(" ".join(parts))

    def state(self):
        from telegram_bot import pending_host
        bot = self.bot
        pf = bot.config.pending_file
        pending = None
        if os.path.exists(pf):
            with open(pf, "rb") as f:
                raw = f.read()
            try:
                usable = isinstance(json.loads(raw.decode("utf-8")), list)
            except ValueError:
                usable = False
            # A file nothing can read is recorded as its bytes, so a row
            # shows whether a press left it alone, rewrote or deleted it.
            pending = [f"{u.get('name')}@{pending_host(u)}"
                       for u in self._pending()] if usable \
                else f"unreadable: {raw!r}"
        # `gone` only where it has a store of its own: on a single host
        # `_store_for` hands every host the raw store, so it would just
        # repeat what `local` shows.
        hosts = ["local"] + (["nas", "gone"] if self.setup == "multi"
                             else [])
        majors = {h: sorted(bot._store_for(h).get_pending_major())
                  for h in hosts}
        return {"pending": pending,
                "queue": [k for k, _ in bot.engine.queued_updates()],
                "stops": len(getattr(bot, "_pending_stops", None) or {}),
                "restores": len(getattr(bot, "_pending_restores", None)
                                or {}),
                "majors": {h: v for h, v in majors.items() if v},
                "locked": bot._update_lock.locked()}


def _join():
    hung, i = [], 0
    while i < len(m.STARTED):                       # threads may start threads
        m.STARTED[i].join(20)
        if m.STARTED[i].is_alive():
            hung.append(m.STARTED[i].name)
        i += 1
    del m.STARTED[:]
    return hung


def run_scenario(setup, steps):
    """Drive one scenario; return (record, escapes)."""
    import hosts as hostslib
    hostslib.forget_unreachable()
    m.EVENTS.clear()
    del m.ESCAPES[:]
    del m.STARTED[:]
    del m.THREAD_ERRORS[:]
    row = Row(setup)
    m.EVENTS.clear()                # build() leaves nothing worth pinning
    for b in row.backends.values():
        del b.calls[:]
    del row.log[:]
    out_steps = []
    sink = io.StringIO()
    for step in steps:
        mark_log = len(row.log)
        mark_calls = {n: len(b.calls) for n, b in row.backends.items()}
        m.EVENTS.clear()
        del m.THREAD_ERRORS[:]
        raised = None
        with contextlib.redirect_stdout(sink), \
                contextlib.redirect_stderr(sink):
            try:
                row.do(step)
            except Exception as e:                  # noqa: BLE001
                raised = type(e).__name__
            hung = _join()
        out = []
        for kind, body, kb in row.log[mark_log:]:
            line = row.norm(body) if kind == "send" \
                else f"<{kind}> {row.norm(body)}".rstrip()
            if kb:
                line += f"  {row.norm(kb)}"
            out.append(line)
        touched = {n: m._calls_summary(b.calls[mark_calls[n]:])
                   for n, b in row.backends.items()
                   if len(b.calls) > mark_calls[n]}
        events = [row.norm(e) for e in m.EVENTS] + list(m.THREAD_ERRORS) \
            + [f"thread did not finish: {h}" for h in hung]
        out_steps.append({"do": row.label(step), "out": out,
                          "hosts": touched, "events": events,
                          "raised": raised})
    state = row.state()
    if state["locked"]:
        row.bot._update_lock.release()
    escapes = list(m.ESCAPES)
    shutil.rmtree(row.data, ignore_errors=True)
    return {"steps": out_steps, "state": state}, escapes


def all_rows():
    return [(s, name) for s in m.SETUPS for name, _ in SCENARIOS]


_STEPS = dict(SCENARIOS)


def _expected_for(setup, name):
    if (setup, name) in EXPECTED:
        return EXPECTED[(setup, name)]
    if setup in ("none", "one"):
        return EXPECTED.get(("single", name))
    return None


# ── checks ─────────────────────────────────────────────────────────────
def trap_checks():
    """The trap, driven through this entry path: a call past the
    stand-ins made inside `_handle_callback` and swallowed there still
    lands in ESCAPES, and an exception inside a thread a press started
    still lands in the step's events."""
    import telegram_bot
    results = []
    real_sweep = telegram_bot.TelegramBot._sweep_restores

    def leaky(self):
        try:
            import subprocess
            subprocess.run(["docker", "info"])
        except Exception:                           # noqa: BLE001
            pass                                    # swallowed on purpose
        return real_sweep(self)
    telegram_bot.TelegramBot._sweep_restores = leaky
    try:
        rec, esc = run_scenario("none", [("tap", "bogus")])
    finally:
        telegram_bot.TelegramBot._sweep_restores = real_sweep
    results.append(("the trap catches a swallowed subprocess.run inside "
                    "_handle_callback",
                    len(esc) == 1 and esc[0].startswith("subprocess.run")))

    real_single = telegram_bot.TelegramBot._run_single_update

    def boom(self, *a, **k):
        raise RuntimeError("inside the update thread")
    telegram_bot.TelegramBot._run_single_update = boom
    try:
        rec, esc = run_scenario("none", [("tap", "update_one:web")])
    finally:
        telegram_bot.TelegramBot._run_single_update = real_single
    results.append(("an exception inside a press's thread is recorded",
                    rec["steps"][0]["events"] == ["RuntimeError in thread"]
                    and not esc))
    return results


#: Q6, written by hand before the change: the two presses that read the
#: pending file themselves (`_run_single_update` and the bare
#: "Update all" through `run_updates`) raised inside their thread on a
#: file they could not read, and the chat heard nothing after the toast.
#: Now they say what a missing file makes them say, update nothing, and
#: leave the file as they found it. The tap is the scenario's last step.
def _q6_spec():
    from i18n import get_translator
    en = get_translator("en")
    return {
        "update_one unreadable file": [
            "<toast> Update web...",            # literal in the bot
            "<api> editMessageReplyMarkup  []",
            en("no_pending_updates")],
        "update_all bare unreadable file": [
            "<unbutton>", "<toast> " + en("updates_starting_cb"),
            en("no_pending_updates")],
    }


def q6_checks():
    results = []
    unreadable = "unreadable: " + repr(m.Q6_FILES[_CUT])
    for name, want in _q6_spec().items():
        for setup in m.SETUPS:
            rec, esc = run_scenario(setup, _STEPS[name])
            tap = rec["steps"][-1]
            results.append((
                f"Q6 [{setup}] {name}: the tap says nothing is pending, "
                f"raises nothing in its thread, updates nothing",
                tap["out"] == want and tap["events"] == []
                and tap["raised"] is None and not esc,
                f"got {tap['out']!r}, events {tap['events']!r}, "
                f"escapes {esc!r}"))
            results.append((
                f"Q6 [{setup}] {name}: the file is left as it was, the "
                f"lock is free",
                rec["state"]["pending"] == unreadable
                and not rec["state"]["locked"],
                f"state {rec['state']!r}"))
    return results


#: Q16, written by hand before the change. With the lock held, "Update
#: all" (the button, bare or with its snapshot), a typed /update and
#: /updateall put their containers on the engine's queue instead of
#: answering "already in progress"; the drainer runs them once the lock
#: comes back through the door. A typed /update asks nobody while it is
#: busy: no lookup on a host, no `check_all` (#26: a check during an
#: update sees the containers in flight as outdated), so only what the
#: pending list already holds is queued, and a name it does not hold
#: gets today's "busy, try again" (my decision of 18.11.2026). A major
#: confirmation is still refused, in words that say so (same decision).
_Q16_HEAD = ("🕒 Another update is running right now. These wait in the "
             "queue and run when their turn comes:")


def _q16_head_behind(name):
    return (f"🕒 `{name}` is being updated right now. These wait in the "
            "queue and run when their turn comes:")


def _q16_line(name, pos):
    return f"• `{name}` — position {pos}"


def _q16_one(name, pos):
    return f"🕒 `{name}` queued — position {pos}. It runs when its turn comes."


_Q16_BUSY = "⚠️ Update already in progress..."
_Q16_MAJOR = ("⚠️ Another update is running, so the major update of `web` "
              "did not start. It was not queued either and still waits for "
              "your confirmation — confirm it again once that update is "
              "done.  [✅ Confirm→confirm_major:web | ❌ Skip→reject_major:web]")
#: The confirm callback took the prompt's buttons away before it knew the
#: lock was held, and no command offers them again: the refusal carries
#: Confirm/Skip itself, for the same key (Q16 review).
_Q16_MAJOR_NAS = _Q16_MAJOR.replace("`web`", "`web` @nas").replace(
    "_major:web", "_major:nas/web")
#: A busy glob queues only what the pending list holds; the rest it matches
#: was not checked, and the answer says so (Q16 review).
_Q16_GLOB_NOTE = ("Only containers already known to have an update were "
                  "queued. Other matches for `web*` were not checked — send "
                  "`/update web*` again once the update is done to check "
                  "them too.")


def _q16_spec():
    from update_engine import UpdateEngine
    mx = UpdateEngine.UPDATE_QUEUE_MAX
    both = "\n".join([_Q16_HEAD, _q16_line("web", 1),
                      _q16_line("web @nas", 2)])
    full = (f"⚠️ The update queue is full ({mx}) — none of these 2 was "
            "queued; anything already waiting keeps its place. Try again "
            "once it has worked through.")
    one = _q16_one("web", 1)
    # (scenario, setups) → (index of the busy step, its last line,
    # the queue after the row)
    return {
        ("update_all lock held", ("multi",)): (2, both, ["web", "nas/web"]),
        ("update_all lock held", ("none", "one")): (2, one, ["web"]),
        ("update_all bare lock held", ("multi",)):
            (1, both, ["web", "nas/web"]),
        ("update_all bare lock held", ("none", "one")): (1, one, ["web"]),
        ("update_all lock held, web being updated", ("multi",)):
            (3, "\n".join([_q16_head_behind("web"),
                           "• `web` — that one is being updated right now",
                           _q16_line("web @nas", 1)]), ["nas/web"]),
        ("update_all lock held, web being updated", ("none", "one")):
            (3, "🕒 `web` is being updated right now — that is the same "
                "one, not a second job.", []),
        ("/update web lock held", m.SETUPS): (1, one, ["web"]),
        ("/update db lock held", m.SETUPS): (1, _Q16_BUSY, []),
        ("/update web @all lock held", ("multi",)):
            (1, both, ["web", "nas/web"]),
        ("/update web @all lock held", ("none", "one")): (1, _Q16_BUSY, []),
        ("/updateall lock held", m.SETUPS): (1, one, ["web"]),
        ("/updateall @nas lock held", ("multi",)):
            (1, _q16_one("web @nas", 1), ["nas/web"]),
        ("confirm_major lock held", m.SETUPS): (2, _Q16_MAJOR, []),
        ("confirm_major:nas/web lock held", ("multi",)):
            (2, _Q16_MAJOR_NAS, []),
        ("/update web* lock held, then released", m.SETUPS):
            (1, one + "\n" + _Q16_GLOB_NOTE, []),
        # Not in the table: a key already waiting inside a batch, and a
        # busy "Update all" with nothing pending at all.
        ("already queued", ("multi",)):
            (3, "\n".join([_Q16_HEAD,
                           "• `web` — already waiting for 0s",
                           _q16_line("web @nas", 2)]), ["web", "nas/web"]),
        ("nothing pending", m.SETUPS):
            (2, "⚠️ No pending updates found.", []),
        # Not in the table: 49 queued keys would fill a screen of it.
        ("queue full", ("multi",)): (3, full, [f"q{i}" for i in range(mx - 1)]),
        ("queue full", ("none", "one")):
            (3, _q16_one("web", mx), [f"q{i}" for i in range(mx - 1)]
             + ["web"]),
        # A busy glob whose match was not queued says why and nothing
        # more: "were queued" under "queue full" or "that is the same
        # one" reads as if part of it had gone through (Q16 review 2).
        ("glob, queue full", m.SETUPS):
            (2, f"⚠️ Queue is full ({mx}). Wait for it to work through, "
                "then tap again.", [f"q{i}" for i in range(mx)]),
        ("glob, web being updated", m.SETUPS):
            (2, "🕒 `web` is being updated right now — that is the same "
                "one, not a second job.", []),
        # Something waits — queued now or earlier: then the note is true
        # and stays.
        ("glob, web already queued", m.SETUPS):
            (2, "🕒 `web` has been in the queue for 0s — it runs when its "
                "turn comes.\n" + _Q16_GLOB_NOTE, ["web"]),
        ("glob @all, web being updated", ("multi",)):
            (2, "\n".join([_q16_head_behind("web"),
                           "• `web` — that one is being updated right now",
                           _q16_line("web @nas", 1),
                           _Q16_GLOB_NOTE.replace("web*", "web* @all")]),
             ["nas/web"]),
    }


def _q16_steps(name):
    if name == "already queued":
        return [("notify", None), ("lock",), ("tap", "update_one:web"),
                ("tap", "update_all:<tok>"), ("unlock",)]
    if name == "nothing pending":
        return [("drop_pending",), ("lock",), ("tap", "update_all"),
                ("unlock",)]
    if name == "glob, queue full":
        from update_engine import UpdateEngine
        return [("lock",), ("fill", UpdateEngine.UPDATE_QUEUE_MAX),
                ("type", "/update web*"), ("unlock",)]
    if name == "glob, web being updated":
        return [("lock",), ("mark_updating", "web"),
                ("type", "/update web*"), ("unlock",)]
    if name == "glob, web already queued":
        return [("lock",), ("type", "/update web"),
                ("type", "/update web*"), ("unlock",)]
    if name == "glob @all, web being updated":
        return [("lock",), ("mark_updating", "web"),
                ("type", "/update web* @all"), ("unlock",)]
    if name == "queue full":
        from update_engine import UpdateEngine
        return [("notify", None), ("lock",),
                ("fill", UpdateEngine.UPDATE_QUEUE_MAX - 1),
                ("tap", "update_all:<tok>"), ("unlock",)]
    return _STEPS[name]


#: Released through the door: the drainer ran what was queued. A typed
#: /update without `@host` stays on the local host, so nas's `web` is
#: still pending afterwards (`_Q16_LEFT`).
_Q16_LEFT = {("/update web* lock held, then released", "multi"): ["web@nas"]}
_Q16_RELEASED = {
    "update_all lock held, then released":
        {"multi": ["update_container@local:web", "update_container@nas:web"],
         "single": ["update_container@local:web"]},
    "/update web* lock held, then released":
        {"multi": ["update_container@local:web"],
         "single": ["update_container@local:web"]},
    "/updateall @all lock held, then released":
        {"multi": ["update_container@local:web", "update_container@nas:web"]},
}


def q16_checks():
    results = []
    for (name, setups), (idx, want, queue) in _q16_spec().items():
        for setup in setups:
            rec, esc = run_scenario(setup, _q16_steps(name))
            st = rec["steps"][idx]
            last = st["out"][-1] if st["out"] else None
            results.append((
                f"Q16 [{setup}] {name}: the answer",
                last == want and st["raised"] is None and not esc,
                f"got {st['out']!r}, raised {st['raised']!r}, "
                f"escapes {esc!r}"))
            results.append((
                f"Q16 [{setup}] {name}: nothing asked of a host, nothing "
                f"checked or updated while busy",
                st["hosts"] == {} and st["events"] == [],
                f"hosts {st['hosts']!r}, events {st['events']!r}"))
            results.append((
                f"Q16 [{setup}] {name}: the queue afterwards",
                rec["state"]["queue"] == queue,
                f"queue {rec['state']['queue']!r}"))
            if name == "confirm_major lock held":
                results.append((
                    f"Q16 [{setup}] {name}: the major still waits",
                    rec["state"]["majors"] == {"local": ["web"]},
                    f"majors {rec['state']['majors']!r}"))
    # The Confirm under the refusal works once the lock is free: it runs
    # the major update of that container on its host and clears it.
    for setup in m.SETUPS:
        name = "confirm_major lock held, confirmed again from its answer"
        rec, esc = run_scenario(setup, _STEPS[name])
        again = rec["steps"][-1]
        ran = [e for e in again["events"] if e.startswith("update_container@")]
        results.append((
            f"Q16 [{setup}] {name}: the second Confirm updates web and "
            f"clears the major",
            ran == ["update_container@local:web"]
            and rec["state"]["majors"] == {} and again["raised"] is None
            and not esc and not rec["state"]["locked"],
            f"ran {ran!r}, events {again['events']!r}, "
            f"state {rec['state']!r}, escapes {esc!r}"))
    for name, per in _Q16_RELEASED.items():
        for setup in m.SETUPS:
            want = per.get("multi" if setup == "multi" else "single")
            if want is None:
                continue
            rec, esc = run_scenario(setup, _STEPS[name])
            busy, rel = rec["steps"][-2], rec["steps"][-1]
            results.append((
                f"Q16 [{setup}] {name}: nothing ran while it was held",
                busy["events"] == [] and busy["hosts"] == {},
                f"events {busy['events']!r}, hosts {busy['hosts']!r}"))
            ran = [e for e in rel["events"]
                   if e.startswith("update_container@")]
            results.append((
                f"Q16 [{setup}] {name}: the release ran each of them once",
                ran == want and not esc and rel["raised"] is None,
                f"ran {ran!r}, events {rel['events']!r}, escapes {esc!r}"))
            left = _Q16_LEFT.get((name, setup), [])
            results.append((
                f"Q16 [{setup}] {name}: nothing left queued or locked, "
                f"pending only {left}",
                rec["state"]["queue"] == []
                and (rec["state"]["pending"] or []) == left
                and not rec["state"]["locked"],
                f"state {rec['state']!r}"))
    return results


def check():
    failed = 0
    for label, ok in trap_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failed += not ok
    for label, ok, detail in q6_checks() + q16_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            print(f"      → {detail}")
        failed += not ok
    for setup, name in all_rows():
        rec, esc = run_scenario(setup, _STEPS[name])
        want = _expected_for(setup, name)
        label = f"[{setup}] {name}"
        if (setup, name) in KNOWN_DEFECTS:
            label += f"  (known defect, {KNOWN_DEFECTS[(setup, name)]})"
        elif (setup, name) in PINNED_FOR:
            label += f"  (pinned for {PINNED_FOR[(setup, name)]})"
        ok = want is not None and rec == want and not esc
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if ok:
            continue
        failed += 1
        if want is None:
            print("      → no expectation for this row")
        if esc:
            print(f"      → escaped the stand-ins: {esc}")
        if want is None or rec == want:
            continue
        if rec["state"] != want["state"]:
            print(f"      → state: expected {want['state']!r}")
            print(f"      →   got      {rec['state']!r}")
        for i in range(max(len(rec["steps"]), len(want["steps"]))):
            got = rec["steps"][i] if i < len(rec["steps"]) else None
            exp = want["steps"][i] if i < len(want["steps"]) else None
            if got == exp:
                continue
            print(f"      → step {i + 1} ({(exp or got)['do']}):")
            for k in ("out", "hosts", "events", "raised"):
                if (got or {}).get(k) != (exp or {}).get(k):
                    print(f"      →   {k}: expected {(exp or {}).get(k)!r}")
                    print(f"      →   {k}: got      {(got or {}).get(k)!r}")
    rows = set(all_rows())
    for key in sorted(k for k in EXPECTED
                      if not ((k[0] == "single" and ("none", k[1]) in rows)
                              or k in rows)):
        print(f"  FAIL  expectation without a row: {key!r}")
        failed += 1
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


def snapshot():
    """Print the EXPECTED table for the code under --app."""
    recs, bad = {}, []
    for setup, name in all_rows():
        rec, esc = run_scenario(setup, _STEPS[name])
        if esc:
            bad.append((setup, name, esc))
        recs[(setup, name)] = rec
    for setup, name, esc in bad:
        print(f"# ESCAPED in {setup} {name!r}: {esc}")
    print("EXPECTED = {")
    seen = set()
    for name, _ in SCENARIOS:
        fam = _family(name)
        if fam not in seen:
            seen.add(fam)
            print(f"    # ── {fam} " + "─" * max(4, 60 - len(fam)))
            import textwrap
            for line in textwrap.wrap(BRANCH_NOTES.get(fam, ""), 70):
                print(f"    # {line}")
        keys = [("multi", name)]
        if recs[("none", name)] == recs[("one", name)]:
            keys.append(("single", name))
        else:
            keys += [("none", name), ("one", name)]
        for k in keys:
            src = recs[("none", name)] if k[0] == "single" else recs[k]
            body = pprint.pformat(src, width=88, sort_dicts=False)
            body = "\n".join("    " + ln if i else ln
                             for i, ln in enumerate(body.splitlines()))
            print(f"    ({k[0]!r}, {name!r}): {body},")
    print("}")
    return 1 if bad else 0


def main():
    m._TMP_ROOT = tempfile.mkdtemp(prefix="tg-cb-matrix-")
    try:
        if "--snapshot" in sys.argv:
            return snapshot()
        return check()
    finally:
        shutil.rmtree(m._TMP_ROOT, ignore_errors=True)


# ── expected, generated by --snapshot from main (8a74ba7), reviewed ──
# ── the Q6 rows (an unreadable pending file, the `[]` a single ──────
# ── update leaves) from core-split after Q6, against q6_checks ──────
EXPECTED = {
    # ── unauthorised chat ───────────────────────────────────────────
    # :3262 `_check_auth` refuses before the audit seam and answers the
    # toast only.
    ('multi', 'unauthorised chat'): {'steps': [{'do': 'tap update_all 7 999',
                'out': ['<api> getChat', '<toast> Not authorized.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'unauthorised chat'): {'steps': [{'do': 'tap update_all 7 999',
                'out': ['<api> getChat', '<toast> Not authorized.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    # ── stop_cancel ─────────────────────────────────────────────────
    # :3288 clears every live stop offer, so the token is gone for the next
    # press.
    ('multi', 'stop_cancel'): {'steps': [{'do': 'type /stop web',
                'out': ['⚠ Stop `web`? It stays down until something starts it again.\n'
                        '\n'
                        'ℹ️ Local host only — add `@<host>` or `@all` for the others.  [🟥 '
                        'Stop→stop_go:<tok> | ✋ Cancel→stop_cancel]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'},
                'events': [],
                'raised': None},
               {'do': 'tap stop_cancel',
                'out': ['<toast> Cancelled — nothing was changed.', '<unbutton>'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap stop_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> ⏳',
                        'That confirmation has expired or was already used — run the '
                        'command again if you still want it.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'stop_cancel'): {'steps': [{'do': 'type /stop web',
                'out': ['⚠ Stop `web`? It stays down until something starts it again.  [🟥 '
                        'Stop→stop_go:<tok> | ✋ Cancel→stop_cancel]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'},
                'events': [],
                'raised': None},
               {'do': 'tap stop_cancel',
                'out': ['<toast> Cancelled — nothing was changed.', '<unbutton>'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap stop_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> ⏳',
                        'That confirmation has expired or was already used — run the '
                        'command again if you still want it.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    # ── stop_go ─────────────────────────────────────────────────────
    # :3295 buttons off first, then the owner gate (fail closed), the TTL,
    # the pop; the press re-runs `lifecycle.act` with the offer's own
    # @token.
    ('multi', 'stop_go owner'): {'steps': [{'do': 'type /stop web',
                'out': ['⚠ Stop `web`? It stays down until something starts it again.\n'
                        '\n'
                        'ℹ️ Local host only — add `@<host>` or `@all` for the others.  [🟥 '
                        'Stop→stop_go:<tok> | ✋ Cancel→stop_cancel]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'},
                'events': [],
                'raised': None},
               {'do': 'tap stop_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> Running stop…',
                        '🟥 `web` stopped.\n'
                        '\n'
                        'ℹ️ Local host only — add `@<host>` or `@all` for the others.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5, stop --time t=90, inspect '
                                   '--format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap stop_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> ⏳',
                        'That confirmation has expired or was already used — run the '
                        'command again if you still want it.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'stop_go owner'): {'steps': [{'do': 'type /stop web',
                'out': ['⚠ Stop `web`? It stays down until something starts it again.  [🟥 '
                        'Stop→stop_go:<tok> | ✋ Cancel→stop_cancel]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'},
                'events': [],
                'raised': None},
               {'do': 'tap stop_go:<tok>',
                'out': ['<unbutton>', '<toast> Running stop…', '🟥 `web` stopped.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5, stop --time t=90, inspect '
                                   '--format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap stop_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> ⏳',
                        'That confirmation has expired or was already used — run the '
                        'command again if you still want it.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'stop_go @nas'): {'steps': [{'do': 'type /stop web @nas',
                'out': ['⚠ Stop `web`? It stays down until something starts it again.  [🟥 '
                        'Stop→stop_go:<tok> | ✋ Cancel→stop_cancel]'],
                'hosts': {'local': 'inspect --format t=5',
                          'nas': 'ps -a --format, inspect --format t=5, inspect --format '
                                 't=10'},
                'events': [],
                'raised': None},
               {'do': 'tap stop_go:<tok>',
                'out': ['<unbutton>', '<toast> Running stop…', '🟥 `web` stopped. @nas'],
                'hosts': {'nas': 'ps -a --format, inspect --format t=5, inspect --format '
                                 't=10, inspect --format t=5, stop --time t=90, inspect '
                                 '--format t=5'},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'stop_go @nas'): {'steps': [{'do': 'type /stop web @nas',
                'out': ['Container `web @nas` not found.'],
                'hosts': {'local': 'ps -a --format'},
                'events': [],
                'raised': None},
               {'do': 'tap stop_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> ⏳',
                        'That confirmation has expired or was already used — run the '
                        'command again if you still want it.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'stop_go foreign'): {'steps': [{'do': 'type /stop web',
                'out': ['⚠ Stop `web`? It stays down until something starts it again.\n'
                        '\n'
                        'ℹ️ Local host only — add `@<host>` or `@all` for the others.  [🟥 '
                        'Stop→stop_go:<tok> | ✋ Cancel→stop_cancel]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'},
                'events': [],
                'raised': None},
               {'do': 'tap stop_go:<tok> 8',
                'out': ['<unbutton>',
                        '<toast> Only the person who ran the command can confirm it.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap stop_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> Running stop…',
                        '🟥 `web` stopped.\n'
                        '\n'
                        'ℹ️ Local host only — add `@<host>` or `@all` for the others.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5, stop --time t=90, inspect '
                                   '--format t=5'},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'stop_go foreign'): {'steps': [{'do': 'type /stop web',
                'out': ['⚠ Stop `web`? It stays down until something starts it again.  [🟥 '
                        'Stop→stop_go:<tok> | ✋ Cancel→stop_cancel]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'},
                'events': [],
                'raised': None},
               {'do': 'tap stop_go:<tok> 8',
                'out': ['<unbutton>',
                        '<toast> Only the person who ran the command can confirm it.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap stop_go:<tok>',
                'out': ['<unbutton>', '<toast> Running stop…', '🟥 `web` stopped.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5, stop --time t=90, inspect '
                                   '--format t=5'},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'stop_go expired'): {'steps': [{'do': 'type /stop web',
                'out': ['⚠ Stop `web`? It stays down until something starts it again.\n'
                        '\n'
                        'ℹ️ Local host only — add `@<host>` or `@all` for the others.  [🟥 '
                        'Stop→stop_go:<tok> | ✋ Cancel→stop_cancel]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'},
                'events': [],
                'raised': None},
               {'do': 'age', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap stop_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> ⏳',
                        'That confirmation has expired or was already used — run the '
                        'command again if you still want it.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'stop_go expired'): {'steps': [{'do': 'type /stop web',
                'out': ['⚠ Stop `web`? It stays down until something starts it again.  [🟥 '
                        'Stop→stop_go:<tok> | ✋ Cancel→stop_cancel]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'},
                'events': [],
                'raised': None},
               {'do': 'age', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap stop_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> ⏳',
                        'That confirmation has expired or was already used — run the '
                        'command again if you still want it.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'stop_go unknown token'): {'steps': [{'do': 'tap stop_go:0123456789ab',
                'out': ['<unbutton>',
                        '<toast> ⏳',
                        'That confirmation has expired or was already used — run the '
                        'command again if you still want it.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'stop_go unknown token'): {'steps': [{'do': 'tap stop_go:0123456789ab',
                'out': ['<unbutton>',
                        '<toast> ⏳',
                        'That confirmation has expired or was already used — run the '
                        'command again if you still want it.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    # ── restore_cancel ──────────────────────────────────────────────
    # :3343 drops only its own token; the bare legacy form drops nothing, so
    # the offer still restores.
    ('multi', 'restore_cancel:<tok>'): {'steps': [{'do': 'upload 7',
                'out': ['📥 *A backup arrived*\n'
                        '\n'
                        '`backup.json`\n'
                        'From: *matrix* · 2026-11-19T10:00 · vx\n'
                        'Contains: pinned\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on *this* instance. Nothing has been changed yet.  '
                        '[♻️ Restore→restore_go:<tok> | ✋ Cancel→restore_cancel:<tok>]'],
                'hosts': {},
                'events': ["download_file('F1')"],
                'raised': None},
               {'do': 'tap restore_cancel:<tok>',
                'out': ['<toast> Left alone.', '<unbutton>'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap restore_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> ⏳',
                        '⚠️ That offer is gone — send the file again.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'restore_cancel:<tok>'): {'steps': [{'do': 'upload 7',
                'out': ['📥 *A backup arrived*\n'
                        '\n'
                        '`backup.json`\n'
                        'From: *matrix* · 2026-11-19T10:00 · vx\n'
                        'Contains: pinned\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on *this* instance. Nothing has been changed yet.  '
                        '[♻️ Restore→restore_go:<tok> | ✋ Cancel→restore_cancel:<tok>]'],
                'hosts': {},
                'events': ["download_file('F1')"],
                'raised': None},
               {'do': 'tap restore_cancel:<tok>',
                'out': ['<toast> Left alone.', '<unbutton>'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap restore_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> ⏳',
                        '⚠️ That offer is gone — send the file again.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'restore_cancel bare'): {'steps': [{'do': 'upload 7',
                'out': ['📥 *A backup arrived*\n'
                        '\n'
                        '`backup.json`\n'
                        'From: *matrix* · 2026-11-19T10:00 · vx\n'
                        'Contains: pinned\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on *this* instance. Nothing has been changed yet.  '
                        '[♻️ Restore→restore_go:<tok> | ✋ Cancel→restore_cancel:<tok>]'],
                'hosts': {},
                'events': ["download_file('F1')"],
                'raised': None},
               {'do': 'tap restore_cancel',
                'out': ['<toast> Left alone.', '<unbutton>'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap restore_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> Restoring…',
                        '✅ Restored: pinned  [🔄 Restart now→restart_self]'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'restore_cancel bare'): {'steps': [{'do': 'upload 7',
                'out': ['📥 *A backup arrived*\n'
                        '\n'
                        '`backup.json`\n'
                        'From: *matrix* · 2026-11-19T10:00 · vx\n'
                        'Contains: pinned\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on *this* instance. Nothing has been changed yet.  '
                        '[♻️ Restore→restore_go:<tok> | ✋ Cancel→restore_cancel:<tok>]'],
                'hosts': {},
                'events': ["download_file('F1')"],
                'raised': None},
               {'do': 'tap restore_cancel',
                'out': ['<toast> Left alone.', '<unbutton>'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap restore_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> Restoring…',
                        '✅ Restored: pinned  [🔄 Restart now→restart_self]'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    # ── restore_go ──────────────────────────────────────────────────
    # :3362 owner gate before the buttons come off; the sweep at :3256 has
    # already dropped an expired offer.
    ('multi', 'restore_go owner'): {'steps': [{'do': 'upload 7',
                'out': ['📥 *A backup arrived*\n'
                        '\n'
                        '`backup.json`\n'
                        'From: *matrix* · 2026-11-19T10:00 · vx\n'
                        'Contains: pinned\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on *this* instance. Nothing has been changed yet.  '
                        '[♻️ Restore→restore_go:<tok> | ✋ Cancel→restore_cancel:<tok>]'],
                'hosts': {},
                'events': ["download_file('F1')"],
                'raised': None},
               {'do': 'tap restore_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> Restoring…',
                        '✅ Restored: pinned  [🔄 Restart now→restart_self]'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap restore_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> ⏳',
                        '⚠️ That offer is gone — send the file again.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'restore_go owner'): {'steps': [{'do': 'upload 7',
                'out': ['📥 *A backup arrived*\n'
                        '\n'
                        '`backup.json`\n'
                        'From: *matrix* · 2026-11-19T10:00 · vx\n'
                        'Contains: pinned\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on *this* instance. Nothing has been changed yet.  '
                        '[♻️ Restore→restore_go:<tok> | ✋ Cancel→restore_cancel:<tok>]'],
                'hosts': {},
                'events': ["download_file('F1')"],
                'raised': None},
               {'do': 'tap restore_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> Restoring…',
                        '✅ Restored: pinned  [🔄 Restart now→restart_self]'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap restore_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> ⏳',
                        '⚠️ That offer is gone — send the file again.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'restore_go foreign'): {'steps': [{'do': 'upload 7',
                'out': ['📥 *A backup arrived*\n'
                        '\n'
                        '`backup.json`\n'
                        'From: *matrix* · 2026-11-19T10:00 · vx\n'
                        'Contains: pinned\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on *this* instance. Nothing has been changed yet.  '
                        '[♻️ Restore→restore_go:<tok> | ✋ Cancel→restore_cancel:<tok>]'],
                'hosts': {},
                'events': ["download_file('F1')"],
                'raised': None},
               {'do': 'tap restore_go:<tok> 8',
                'out': ['<toast> Only the person who sent this file can restore it.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap restore_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> Restoring…',
                        '✅ Restored: pinned  [🔄 Restart now→restart_self]'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'restore_go foreign'): {'steps': [{'do': 'upload 7',
                'out': ['📥 *A backup arrived*\n'
                        '\n'
                        '`backup.json`\n'
                        'From: *matrix* · 2026-11-19T10:00 · vx\n'
                        'Contains: pinned\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on *this* instance. Nothing has been changed yet.  '
                        '[♻️ Restore→restore_go:<tok> | ✋ Cancel→restore_cancel:<tok>]'],
                'hosts': {},
                'events': ["download_file('F1')"],
                'raised': None},
               {'do': 'tap restore_go:<tok> 8',
                'out': ['<toast> Only the person who sent this file can restore it.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap restore_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> Restoring…',
                        '✅ Restored: pinned  [🔄 Restart now→restart_self]'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'restore_go expired'): {'steps': [{'do': 'upload 7',
                'out': ['📥 *A backup arrived*\n'
                        '\n'
                        '`backup.json`\n'
                        'From: *matrix* · 2026-11-19T10:00 · vx\n'
                        'Contains: pinned\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on *this* instance. Nothing has been changed yet.  '
                        '[♻️ Restore→restore_go:<tok> | ✋ Cancel→restore_cancel:<tok>]'],
                'hosts': {},
                'events': ["download_file('F1')"],
                'raised': None},
               {'do': 'age', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap restore_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> ⏳',
                        '⚠️ That offer is gone — send the file again.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'restore_go expired'): {'steps': [{'do': 'upload 7',
                'out': ['📥 *A backup arrived*\n'
                        '\n'
                        '`backup.json`\n'
                        'From: *matrix* · 2026-11-19T10:00 · vx\n'
                        'Contains: pinned\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on *this* instance. Nothing has been changed yet.  '
                        '[♻️ Restore→restore_go:<tok> | ✋ Cancel→restore_cancel:<tok>]'],
                'hosts': {},
                'events': ["download_file('F1')"],
                'raised': None},
               {'do': 'age', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap restore_go:<tok>',
                'out': ['<unbutton>',
                        '<toast> ⏳',
                        '⚠️ That offer is gone — send the file again.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    # ── restart_self ────────────────────────────────────────────────
    # :3416 `restart_self` with the caller's checker.
    ('multi', 'restart_self'): {'steps': [{'do': 'tap restart_self',
                'out': ['<unbutton>',
                        '<toast> Restarting…',
                        '🔄 Restarting — back in a moment. (restart policy: '
                        '`unless-stopped`)'],
                'hosts': {'local': 'inspect --format t=5 ×2, inspect --format t=15'},
                'events': ['selfrestart.go_down'],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'restart_self'): {'steps': [{'do': 'tap restart_self',
                'out': ['<unbutton>',
                        '<toast> Restarting…',
                        '🔄 Restarting — back in a moment. (restart policy: '
                        '`unless-stopped`)'],
                'hosts': {'local': 'inspect --format t=5 ×2, inspect --format t=15'},
                'events': ['selfrestart.go_down'],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    # ── update_all ──────────────────────────────────────────────────
    # :3423 token → snapshot (stale answers and stops), bare → the whole
    # pending file; `run_updates` on a thread with the caller's (local)
    # checker. Since Q12 it runs one batch per host, in the order the hosts
    # first appear, each with that host's checker: the netns snapshot
    # (`inspect --format`) and a group head's cascade (wait_healthy, the
    # dependents' restart) go to the entry's own host. A host that stops
    # answering fails its own entry from the snapshot; a host the registry
    # does not know keeps the caller's checker and the batch refuses it.
    # Since Q16 a busy lock queues the whole set (snapshot, or the file for
    # the bare form) through `claim_or_queue_many` instead of 'already in
    # progress'; one key gets the tap's sentence, several a heading (naming
    # what is being updated, if anything) and a line each. Released through
    # the door, the drainer runs them.
    ('multi', 'update_all bare'): {'steps': [{'do': 'tap update_all',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '⏳ Starting update for 2 containers...',
                        '*Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)\n'
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format',
                          'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@local:web', 'update_container@nas:web'],
                'raised': None}],
     'state': {'pending': None,
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_all bare'): {'steps': [{'do': 'tap update_all',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '⏳ Starting update for 1 containers...',
                        '*Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': None,
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_all:<tok> local'): {'steps': [{'do': "notify ['local']",
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🚀 Update '
                        'all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '⏳ Starting update for 1 containers...',
                        '*Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': ['web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_all:<tok> local'): {'steps': [{'do': "notify ['local']",
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🚀 Update '
                        'all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '⏳ Starting update for 1 containers...',
                        '*Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': None,
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_all:<tok> mixed'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5',
                          'nas': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '⏳ Starting update for 2 containers...',
                        '*Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)\n'
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format',
                          'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@local:web', 'update_container@nas:web'],
                'raised': None}],
     'state': {'pending': None,
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_all:<tok> mixed'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🚀 Update '
                        'all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '⏳ Starting update for 1 containers...',
                        '*Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': None,
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_all:<tok> mixed, nas group head'): {'steps': [{'do': "group nas g1 ['web', 'media']",
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas 👑 '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web @nas (?)→update_one:nas/web] [🔄 web '
                        '(?)→update_one:web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5',
                          'nas': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '⏳ Starting update for 2 containers...',
                        '*Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)\n'
                        '🔁 `web` dependents restarted: `media`\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format',
                          'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format, inspect --format t=10, restart t=30'},
                'events': ['update_container@nas:web',
                           'wait_healthy@nas:web max=30',
                           'update_container@local:web'],
                'raised': None}],
     'state': {'pending': None,
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_all:<tok> mixed, nas group head'): {'steps': [{'do': "group nas g1 ['web', 'media']",
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) 👑 '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🚀 Update '
                        'all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '⏳ Starting update for 1 containers...',
                        '*Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)\n'
                        '❌ `web` dependents: 1 FAILED\n'
                        '   • `media`: Error: No such container: media'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format, inspect --format t=10 ×3, restart t=30'},
                'events': ['update_container@local:web', 'wait_healthy@local:web max=30'],
                'raised': None}],
     'state': {'pending': None,
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_all:<tok> mixed, dead host'): {'steps': [{'do': 'add_pending web nginx:1.25 dead',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://hub.docker.com/_/nginx) @dead (nginx:1.25) 🔖 '
                        'v1.0.0 → v1.1.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🔄 web @dead (?)→update_one:dead/web] [🚀 '
                        'Update all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5',
                          'nas': 'inspect --format t=10, inspect --format t=5',
                          'dead': 'inspect --format t=10, inspect --format t=5 ×2'},
                'events': [],
                'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '⏳ Starting update for 3 containers...',
                        '*Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)\n'
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)\n'
                        '❌ [web](https://hub.docker.com/_/nginx) @dead: Command '
                        "'['docker', '-H', 'ssh://dead', 'inspect', '--format', "
                        "'{{.HostConfig.NetworkMode}}', 'web']' timed out after 30 "
                        'seconds'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format',
                          'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format',
                          'dead': 'inspect --format t=10, inspect --format t=5 ×2, inspect '
                                  '--format'},
                'events': ['update_container@local:web', 'update_container@nas:web'],
                'raised': None}],
     'state': {'pending': None,
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_all:<tok> mixed, dead host'): {'steps': [{'do': 'add_pending web nginx:1.25 dead',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @dead '
                        '(nginx:1.25) 🔖 v1.0.0 → v1.1.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🔄 web @dead '
                        '(?)→update_one:dead/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '⏳ Starting update for 2 containers...',
                        '*Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)\n'
                        '❌ [web](https://github.com/example/web/releases/latest) @dead: '
                        'host `dead` is not managed any more — skipped rather than acting '
                        'on the wrong machine'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format, inspect --format t=10, inspect --format t=5, '
                                   'inspect --format'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': None,
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_all:<tok> mixed, gone host'): {'steps': [{'do': 'add_pending web nginx:1.25 gone',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @gone '
                        '(nginx:1.25) 🔖 v1.0.0 → v1.1.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🔄 web @gone (?)→update_one:gone/web] [🚀 '
                        'Update all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format t=10, inspect --format t=5',
                          'nas': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '⏳ Starting update for 3 containers...',
                        '*Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)\n'
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)\n'
                        '❌ [web](https://github.com/example/web/releases/latest) @gone: '
                        'host `gone` is not managed any more — skipped rather than acting '
                        'on the wrong machine'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format, inspect --format t=10, inspect --format t=5, '
                                   'inspect --format',
                          'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@local:web', 'update_container@nas:web'],
                'raised': None}],
     'state': {'pending': None,
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_all:<tok> mixed, gone host'): {'steps': [{'do': 'add_pending web nginx:1.25 gone',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @gone '
                        '(nginx:1.25) 🔖 v1.0.0 → v1.1.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🔄 web @gone '
                        '(?)→update_one:gone/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '⏳ Starting update for 2 containers...',
                        '*Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)\n'
                        '❌ [web](https://github.com/example/web/releases/latest) @gone: '
                        'host `gone` is not managed any more — skipped rather than acting '
                        'on the wrong machine'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format, inspect --format t=10, inspect --format t=5, '
                                   'inspect --format'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': None,
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_all:<tok> stale'): {'steps': [{'do': 'tap update_all:7',
                'out': ['<toast> This notification is stale',
                        '<unbutton>',
                        '⚠️ That "Updates Available" notification is too old to act on (a '
                        'newer check has replaced it, or the bot restarted). Run /check '
                        'for a fresh list.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_all:<tok> stale'): {'steps': [{'do': 'tap update_all:7',
                'out': ['<toast> This notification is stale',
                        '<unbutton>',
                        '⚠️ That "Updates Available" notification is too old to act on (a '
                        'newer check has replaced it, or the bot restarted). Run /check '
                        'for a fresh list.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_all lock held'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5',
                          'nas': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '🕒 Another update is running right now. These wait in the queue '
                        'and run when their turn comes:\n'
                        '• `web` — position 1\n'
                        '• `web @nas` — position 2'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': ['web', 'nas/web'],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_all lock held'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🚀 Update '
                        'all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '🕒 `web` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': ['web'],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_all lock held, then released'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5',
                          'nas': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '🕒 Another update is running right now. These wait in the queue '
                        'and run when their turn comes:\n'
                        '• `web` — position 1\n'
                        '• `web @nas` — position 2'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'release',
                'out': ['⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '⏳ Updating `web @nas`...',
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)',
                        '✅ All updates completed.'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format',
                          'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@local:web', 'update_container@nas:web'],
                'raised': None}],
     'state': {'pending': [],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_all lock held, then released'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🚀 Update '
                        'all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '🕒 `web` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'release',
                'out': ['⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '✅ All updates completed.'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': [],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_all bare lock held'): {'steps': [{'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_all',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '🕒 Another update is running right now. These wait in the queue '
                        'and run when their turn comes:\n'
                        '• `web` — position 1\n'
                        '• `web @nas` — position 2'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': ['web', 'nas/web'],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_all bare lock held'): {'steps': [{'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_all',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '🕒 `web` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': ['web'],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_all lock held, web being updated'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5',
                          'nas': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'mark_updating web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '🕒 `web` is being updated right now. These wait in the queue and '
                        'run when their turn comes:\n'
                        '• `web` — that one is being updated right now\n'
                        '• `web @nas` — position 1'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': ['nas/web'],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_all lock held, web being updated'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🚀 Update '
                        'all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'mark_updating web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '🕒 `web` is being updated right now — that is the same one, not a '
                        'second job.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    # ── update_skip ─────────────────────────────────────────────────
    # :3451 multi keeps the other hosts' entries, single deletes the file.
    ('multi', 'update_skip'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5',
                          'nas': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_skip',
                'out': ['<unbutton>',
                        '<toast> OK, manual.',
                        '👍 Updates will not be performed automatically.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_skip'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🚀 Update '
                        'all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_skip',
                'out': ['<unbutton>',
                        '<toast> OK, manual.',
                        '👍 Updates will not be performed automatically.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': None,
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    # ── update_one ──────────────────────────────────────────────────
    # :3479 rebuilds the keyboard without the tapped button (`noop`),
    # `_run_single_update` on a thread; it reassigns `checker` to the tapped
    # key's host (:1798). Until Q7b it handed that one to the drain (:1929),
    # and `_checker_for` returns the caller's checker for the local host, so
    # a queued local key ran on nas's; now the drain gets the checker the
    # tap was called with. A pending key whose host the registry does not
    # know (`gone`) is refused in `run_keys` since Q11b: 'Unknown host',
    # nothing runs, the entry stays. Since Q11c `run_keys` runs each key
    # through the shared batch: the netns snapshot asks the tapped key's own
    # host (the extra `inspect --format`), a group head's dependents are
    # restarted, and a queued key waits the last recreate's cooldown and its
    # group's pause first.
    ('multi', 'update_one:web'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5',
                          'nas': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  [✅ web (?)→noop] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]',
                        '⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': ['web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_one:web'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🚀 Update '
                        'all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  [✅ web (?)→noop]',
                        '⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '✅ All updates completed.'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': [],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_one:nas/web'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5',
                          'nas': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_one:nas/web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  [🔄 web (?)→update_one:web] [✅ web '
                        '@nas (?)→noop] [🚀 Update all→update_all:1 | ✋ Manual→update_skip]',
                        '⏳ Updating `web @nas`...',
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)'],
                'hosts': {'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@nas:web'],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_one:nas/web'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🚀 Update '
                        'all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_one:nas/web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  []',
                        '⚠️ Container `web @nas` not in update list.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_one not pending'): {'steps': [{'do': 'tap update_one:db',
                'out': ['<toast> Update db...',
                        '<api> editMessageReplyMarkup  []',
                        '⚠️ Container `db` not in update list.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_one not pending'): {'steps': [{'do': 'tap update_one:db',
                'out': ['<toast> Update db...',
                        '<api> editMessageReplyMarkup  []',
                        '⚠️ Container `db` not in update list.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_one gone host'): {'steps': [{'do': 'add_pending web nginx:1.25 gone',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap update_one:gone/web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  []',
                        '❌ Unknown host `gone`. Managed hosts: `local`, `nas`, `dead`, '
                        '`refused`, `flaky`'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas', 'web@gone'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_one gone host'): {'steps': [{'do': 'add_pending web nginx:1.25 gone',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap update_one:gone/web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  []',
                        '❌ Unknown host `gone`. Managed hosts: `local`'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@gone'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_one empty file'): {'steps': [{'do': 'drop_pending', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  []',
                        '⚠️ No pending updates found.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': None,
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_one empty file'): {'steps': [{'do': 'drop_pending', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  []',
                        '⚠️ No pending updates found.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': None,
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_one after the last single update'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5',
                          'nas': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  [✅ web (?)→noop] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]',
                        '⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web'],
                'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  [✅ web (?)→noop] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]',
                        '⚠️ Container `web` not in update list.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_one after the last single update'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🚀 Update '
                        'all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  [✅ web (?)→noop]',
                        '⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '✅ All updates completed.'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web'],
                'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  [✅ web (?)→noop]',
                        '⚠️ Container `web` not in update list.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': [],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_one unreadable file'): {'steps': [{'do': 'raw_pending b\'[{"name": "web", "image": "nginx:1.25", "ho\'',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  []',
                        '⚠️ No pending updates found.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': 'unreadable: b\'[{"name": "web", "image": "nginx:1.25", "ho\'',
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_one unreadable file'): {'steps': [{'do': 'raw_pending b\'[{"name": "web", "image": "nginx:1.25", "ho\'',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  []',
                        '⚠️ No pending updates found.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': 'unreadable: b\'[{"name": "web", "image": "nginx:1.25", "ho\'',
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_all bare unreadable file'): {'steps': [{'do': 'raw_pending b\'[{"name": "web", "image": "nginx:1.25", "ho\'',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap update_all',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '⚠️ No pending updates found.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': 'unreadable: b\'[{"name": "web", "image": "nginx:1.25", "ho\'',
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_all bare unreadable file'): {'steps': [{'do': 'raw_pending b\'[{"name": "web", "image": "nginx:1.25", "ho\'',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap update_all',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '⚠️ No pending updates found.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': 'unreadable: b\'[{"name": "web", "image": "nginx:1.25", "ho\'',
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_all:<tok> unreadable file'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5',
                          'nas': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'raw_pending b\'[{"name": "web", "image": "nginx:1.25", "ho\'',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '⏳ Starting update for 2 containers...',
                        '*Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)\n'
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format',
                          'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@local:web', 'update_container@nas:web'],
                'raised': None}],
     'state': {'pending': 'unreadable: b\'[{"name": "web", "image": "nginx:1.25", "ho\'',
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_all:<tok> unreadable file'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🚀 Update '
                        'all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'raw_pending b\'[{"name": "web", "image": "nginx:1.25", "ho\'',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap update_all:<tok>',
                'out': ['<unbutton>',
                        '<toast> Updates are starting...',
                        '⏳ Starting update for 1 containers...',
                        '*Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': 'unreadable: b\'[{"name": "web", "image": "nginx:1.25", "ho\'',
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_skip unreadable file'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5',
                          'nas': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'raw_pending b\'[{"name": "web", "image": "nginx:1.25", "ho\'',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap update_skip',
                'out': ['<unbutton>',
                        '<toast> OK, manual.',
                        '👍 Updates will not be performed automatically.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': None,
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_skip unreadable file'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🚀 Update '
                        'all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'raw_pending b\'[{"name": "web", "image": "nginx:1.25", "ho\'',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap update_skip',
                'out': ['<unbutton>',
                        '<toast> OK, manual.',
                        '👍 Updates will not be performed automatically.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': None,
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    # ── queue ───────────────────────────────────────────────────────
    # :1825 lock busy → enqueue (position, then 'already in the queue').
    # Since Q13 the engine's drainer works it off once the tap that held the
    # lock releases it, whatever that tap did: one that ran nothing (key not
    # listed, so on a single host the remote tap) used to return before the
    # drain, and the queued `db` waited for the next sweep. Now it runs
    # right after.
    ('multi', 'queue drain after a remote tap'): {'steps': [{'do': 'add_pending db postgres:16 local',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:db',
                'out': ['<toast> Update db...',
                        '<api> editMessageReplyMarkup  []',
                        '🕒 `db` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:nas/web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  []',
                        '⏳ Updating `web @nas`...',
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)',
                        '⏳ Updating `db`...',
                        '✅ [db](https://hub.docker.com/_/postgres): updated (stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5 ×2, '
                                   'inspect --format',
                          'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@nas:web', 'update_container@local:db'],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'queue drain after a remote tap'): {'steps': [{'do': 'add_pending db postgres:16 local',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:db',
                'out': ['<toast> Update db...',
                        '<api> editMessageReplyMarkup  []',
                        '🕒 `db` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:nas/web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  []',
                        '⚠️ Container `web @nas` not in update list.',
                        '⏳ Updating `db`...',
                        '✅ [db](https://hub.docker.com/_/postgres): updated (stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5 ×2, '
                                   'inspect --format'},
                'events': ['update_container@local:db'],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'queue drain after a local tap'): {'steps': [{'do': 'add_pending db postgres:16 local',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:nas/web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  []',
                        '🕒 `web @nas` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:db',
                'out': ['<toast> Update db...',
                        '<api> editMessageReplyMarkup  []',
                        '⏳ Updating `db`...',
                        '✅ [db](https://hub.docker.com/_/postgres): updated (stand-in)',
                        '⏳ Updating `web @nas`...',
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5 ×2, '
                                   'inspect --format',
                          'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@local:db', 'update_container@nas:web'],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'queue drain after a local tap'): {'steps': [{'do': 'add_pending db postgres:16 local',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:nas/web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  []',
                        '🕒 `web @nas` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:db',
                'out': ['<toast> Update db...',
                        '<api> editMessageReplyMarkup  []',
                        '⏳ Updating `db`...',
                        '✅ [db](https://hub.docker.com/_/postgres): updated (stand-in)',
                        '✅ `web @nas` was already updated while this one waited. Nothing '
                        'left to do.'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5 ×2, '
                                   'inspect --format'},
                'events': ['update_container@local:db'],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'queue twice while locked'): {'steps': [{'do': 'add_pending db postgres:16 local',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:db',
                'out': ['<toast> Update db...',
                        '<api> editMessageReplyMarkup  []',
                        '🕒 `db` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap update_one:db',
                'out': ['<toast> Update db...',
                        '<api> editMessageReplyMarkup  []',
                        '🕒 `db` has been in the queue for 0s — it runs when its turn '
                        'comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local', 'web@nas', 'db@local'],
               'queue': ['db'],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'queue twice while locked'): {'steps': [{'do': 'add_pending db postgres:16 local',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:db',
                'out': ['<toast> Update db...',
                        '<api> editMessageReplyMarkup  []',
                        '🕒 `db` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap update_one:db',
                'out': ['<toast> Update db...',
                        '<api> editMessageReplyMarkup  []',
                        '🕒 `db` has been in the queue for 0s — it runs when its turn '
                        'comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local', 'db@local'],
               'queue': ['db'],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    # Q11c, written by hand from the rows before it: the tap runs through
    # the batch (netns snapshot, cascade, the carried cooldown and group pause).
    ('multi', 'queue drain after a cooldown container'): {'steps': [{'do': 'cooldown local web 60',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'add_pending db postgres:16 local',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:db',
                'out': ['<toast> Update db...',
                        '<api> editMessageReplyMarkup  []',
                        '🕒 `db` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  []',
                        '⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '⏳ Updating `db`...',
                        '✅ [db](https://hub.docker.com/_/postgres): updated (stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format, inspect --format t=10, inspect --format t=5 '
                                   '×2, inspect --format'},
                'events': ['update_container@local:web',
                           'wait 60s',
                           'update_container@local:db'],
                'raised': None}],
     'state': {'pending': ['web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'queue drain after a cooldown container'): {'steps': [{'do': 'cooldown local web 60',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'add_pending db postgres:16 local',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:db',
                'out': ['<toast> Update db...',
                        '<api> editMessageReplyMarkup  []',
                        '🕒 `db` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  []',
                        '⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '⏳ Updating `db`...',
                        '✅ [db](https://hub.docker.com/_/postgres): updated (stand-in)',
                        '✅ All updates completed.'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format, inspect --format t=10, inspect --format t=5 '
                                   '×2, inspect --format'},
                'events': ['update_container@local:web',
                           'wait 60s',
                           'update_container@local:db'],
                'raised': None}],
     'state': {'pending': [],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'update_one cooldown container, nothing queued'): {'steps': [{'do': 'cooldown local web 60',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5',
                          'nas': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  [✅ web (?)→noop] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]',
                        '⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': ['web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'update_one cooldown container, nothing queued'): {'steps': [{'do': 'cooldown local web 60',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🚀 Update '
                        'all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  [✅ web (?)→noop]',
                        '⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '✅ All updates completed.'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': [],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'queue drain after a group head'): {'steps': [{'do': "group local g1 ['web', 'db']",
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'add_pending db postgres:16 local',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:db',
                'out': ['<toast> Update db...',
                        '<api> editMessageReplyMarkup  []',
                        '🕒 `db` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  []',
                        '⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '🔁 `web` dependents restarted: `db`',
                        '⏳ Updating `db`...',
                        '✅ [db](https://hub.docker.com/_/postgres): updated (stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format, inspect --format t=10, restart t=30, inspect '
                                   '--format t=10, inspect --format t=5 ×2, inspect '
                                   '--format'},
                'events': ['update_container@local:web',
                           'wait_healthy@local:web max=30',
                           'wait 5s',
                           'update_container@local:db'],
                'raised': None}],
     'state': {'pending': ['web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'queue drain after a group head'): {'steps': [{'do': "group local g1 ['web', 'db']",
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'add_pending db postgres:16 local',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:db',
                'out': ['<toast> Update db...',
                        '<api> editMessageReplyMarkup  []',
                        '🕒 `db` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  []',
                        '⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '🔁 `web` dependents restarted: `db`',
                        '⏳ Updating `db`...',
                        '✅ [db](https://hub.docker.com/_/postgres): updated (stand-in)',
                        '✅ All updates completed.'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format, inspect --format t=10, restart t=30, inspect '
                                   '--format t=10, inspect --format t=5 ×2, inspect '
                                   '--format'},
                'events': ['update_container@local:web',
                           'wait_healthy@local:web max=30',
                           'wait 5s',
                           'update_container@local:db'],
                'raised': None}],
     'state': {'pending': [],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    # ── confirm_major ───────────────────────────────────────────────
    # :3497 on a thread, `_confirm_major_update`; busy lock still refuses
    # and does not queue (my decision for Q16), with a sentence that says so
    # and that the major still waits. A key whose host the registry does not
    # know (`gone`, and `nas` on a single host) answers 'Unknown host' since
    # Q11b, before anything is looked up; nothing runs and the deferred
    # major stays.
    ('multi', 'confirm_major:web'): {'steps': [{'do': 'major local web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap confirm_major:web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'confirm_major:web'): {'steps': [{'do': 'major local web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap confirm_major:web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'confirm_major:nas/web'): {'steps': [{'do': 'major nas web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap confirm_major:nas/web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)'],
                'hosts': {'nas': 'inspect --format t=10, inspect --format t=5'},
                'events': ['update_container@nas:web'],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'confirm_major:nas/web'): {'steps': [{'do': 'major nas web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap confirm_major:nas/web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '❌ Unknown host `nas`. Managed hosts: `local`'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {'local': ['web']},
               'locked': False}},
    ('multi', 'confirm_major gone host'): {'steps': [{'do': 'major gone web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap confirm_major:gone/web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '❌ Unknown host `gone`. Managed hosts: `local`, `nas`, `dead`, '
                        '`refused`, `flaky`'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {'gone': ['web']},
               'locked': False}},
    ('single', 'confirm_major gone host'): {'steps': [{'do': 'major gone web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap confirm_major:gone/web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '❌ Unknown host `gone`. Managed hosts: `local`'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {'local': ['web']},
               'locked': False}},
    ('multi', 'confirm_major gone host, nothing deferred'): {'steps': [{'do': 'tap confirm_major:gone/web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '❌ Unknown host `gone`. Managed hosts: `local`, `nas`, `dead`, '
                        '`refused`, `flaky`'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'confirm_major gone host, nothing deferred'): {'steps': [{'do': 'tap confirm_major:gone/web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '❌ Unknown host `gone`. Managed hosts: `local`'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'confirm_major none pending'): {'steps': [{'do': 'tap confirm_major:web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '⚠️ No pending major update for `web`.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'confirm_major none pending'): {'steps': [{'do': 'tap confirm_major:web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '⚠️ No pending major update for `web`.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'confirm_major lock held'): {'steps': [{'do': 'major local web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap confirm_major:web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '⚠️ Another update is running, so the major update of `web` did '
                        'not start. It was not queued either and still waits for your '
                        'confirmation — confirm it again once that update is done.  [✅ '
                        'Confirm→confirm_major:web | ❌ Skip→reject_major:web]'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {'local': ['web']},
               'locked': False}},
    ('single', 'confirm_major lock held'): {'steps': [{'do': 'major local web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap confirm_major:web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '⚠️ Another update is running, so the major update of `web` did '
                        'not start. It was not queued either and still waits for your '
                        'confirmation — confirm it again once that update is done.  [✅ '
                        'Confirm→confirm_major:web | ❌ Skip→reject_major:web]'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {'local': ['web']},
               'locked': False}},
    ('multi', 'confirm_major lock held, confirmed again from its answer'): {'steps': [{'do': 'major local web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap confirm_major:web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '⚠️ Another update is running, so the major update of `web` did '
                        'not start. It was not queued either and still waits for your '
                        'confirmation — confirm it again once that update is done.  [✅ '
                        'Confirm→confirm_major:web | ❌ Skip→reject_major:web]'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap confirm_major:web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'confirm_major lock held, confirmed again from its answer'): {'steps': [{'do': 'major local web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap confirm_major:web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '⚠️ Another update is running, so the major update of `web` did '
                        'not start. It was not queued either and still waits for your '
                        'confirmation — confirm it again once that update is done.  [✅ '
                        'Confirm→confirm_major:web | ❌ Skip→reject_major:web]'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap confirm_major:web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'confirm_major:nas/web lock held'): {'steps': [{'do': 'major nas web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap confirm_major:nas/web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '⚠️ Another update is running, so the major update of `web` @nas '
                        'did not start. It was not queued either and still waits for your '
                        'confirmation — confirm it again once that update is done.  [✅ '
                        'Confirm→confirm_major:nas/web | ❌ Skip→reject_major:nas/web]'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {'nas': ['web']},
               'locked': False}},
    ('single', 'confirm_major:nas/web lock held'): {'steps': [{'do': 'major nas web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'tap confirm_major:nas/web',
                'out': ['<toast> Confirming major update for web…',
                        '<unbutton>',
                        '❌ Unknown host `nas`. Managed hosts: `local`'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {'local': ['web']},
               'locked': False}},
    # ── /update ─────────────────────────────────────────────────────
    # typed, with the lock held (Q16): no lookup on a host and no
    # `check_all`; the pending entries the pattern names (exactly, or as a
    # glob) on the targeted hosts go to `run_updates` on a thread, which
    # queues them. A name the pending list does not hold answers 'already in
    # progress' as before. Without `@host` it stays local, so nas's `web` is
    # still pending after the release.
    ('multi', '/update web lock held'): {'steps': [{'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'type /update web',
                'out': ['🕒 `web` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': ['web'],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', '/update web lock held'): {'steps': [{'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'type /update web',
                'out': ['🕒 `web` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': ['web'],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', '/update web* lock held, then released'): {'steps': [{'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'type /update web*',
                'out': ['🕒 `web` queued — position 1. It runs when its turn comes.\n'
                        'Only containers already known to have an update were queued. '
                        'Other matches for `web*` were not checked — send `/update web*` '
                        'again once the update is done to check them too.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'release',
                'out': ['⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': ['web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', '/update web* lock held, then released'): {'steps': [{'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'type /update web*',
                'out': ['🕒 `web` queued — position 1. It runs when its turn comes.\n'
                        'Only containers already known to have an update were queued. '
                        'Other matches for `web*` were not checked — send `/update web*` '
                        'again once the update is done to check them too.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'release',
                'out': ['⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '✅ All updates completed.'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web'],
                'raised': None}],
     'state': {'pending': [],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', '/update db lock held'): {'steps': [{'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'type /update db',
                'out': ['⚠️ Update already in progress...'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', '/update db lock held'): {'steps': [{'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'type /update db',
                'out': ['⚠️ Update already in progress...'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', '/update web @all lock held'): {'steps': [{'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'type /update web @all',
                'out': ['🕒 Another update is running right now. These wait in the queue '
                        'and run when their turn comes:\n'
                        '• `web` — position 1\n'
                        '• `web @nas` — position 2'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': ['web', 'nas/web'],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', '/update web @all lock held'): {'steps': [{'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'type /update web @all',
                'out': ['⚠️ Update already in progress...'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    # ── /updateall ──────────────────────────────────────────────────
    # typed, with the lock held (Q16): the same set as the free path (the
    # file, or its `@host` part) goes through `_handle_callback` to
    # `run_updates` and is queued. Without `@host` it is the local host on
    # multi as well.
    ('multi', '/updateall lock held'): {'steps': [{'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'type /updateall',
                'out': ['<toast> Updates are starting...',
                        '🕒 `web` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': ['web'],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', '/updateall lock held'): {'steps': [{'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'type /updateall',
                'out': ['<toast> Updates are starting...',
                        '🕒 `web` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': ['web'],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', '/updateall @nas lock held'): {'steps': [{'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'type /updateall @nas',
                'out': ['<toast> Updates are starting...',
                        '🕒 `web @nas` queued — position 1. It runs when its turn comes.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': ['nas/web'],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', '/updateall @nas lock held'): {'steps': [{'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'type /updateall @nas',
                'out': ['`/updateall` updates everything that has an update waiting. It '
                        'takes a host (`@nas`, `@all`) and nothing else — for one '
                        'container use `/update <name>`.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'unlock', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', '/updateall @all lock held, then released'): {'steps': [{'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'type /updateall @all',
                'out': ['<toast> Updates are starting...',
                        '🕒 Another update is running right now. These wait in the queue '
                        'and run when their turn comes:\n'
                        '• `web` — position 1\n'
                        '• `web @nas` — position 2'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'release',
                'out': ['⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '⏳ Updating `web @nas`...',
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)',
                        '✅ All updates completed.'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format',
                          'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@local:web', 'update_container@nas:web'],
                'raised': None}],
     'state': {'pending': [],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', '/updateall @all lock held, then released'): {'steps': [{'do': 'lock', 'out': [], 'hosts': {}, 'events': [], 'raised': None},
               {'do': 'type /updateall @all',
                'out': ['`/updateall` updates everything that has an update waiting. It '
                        'takes a host (`@nas`, `@all`) and nothing else — for one '
                        'container use `/update <name>`.'],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'release', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    # ── reject_major ────────────────────────────────────────────────
    # :3508 drops the deferred major in that host's store. A key whose host
    # the registry does not know (`gone`, and `nas` on a single host)
    # answers 'Unknown host' since Q11b, like confirm_major: before,
    # `_store_for` handed it the local store, so the local `web`'s deferred
    # major went and the chat said 'skipped'.
    ('multi', 'reject_major:web'): {'steps': [{'do': 'major local web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap reject_major:web',
                'out': ['<toast> Skipped major update for web.',
                        '<unbutton>',
                        '⏭ Major update for `web` skipped.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'reject_major:web'): {'steps': [{'do': 'major local web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap reject_major:web',
                'out': ['<toast> Skipped major update for web.',
                        '<unbutton>',
                        '⏭ Major update for `web` skipped.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'reject_major:nas/web'): {'steps': [{'do': 'major nas web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap reject_major:nas/web',
                'out': ['<toast> Skipped major update for web.',
                        '<unbutton>',
                        '⏭ Major update for `web` @nas skipped.'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'reject_major:nas/web'): {'steps': [{'do': 'major nas web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap reject_major:nas/web',
                'out': ['<toast> Skipped major update for web.',
                        '<unbutton>',
                        '❌ Unknown host `nas`. Managed hosts: `local`'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {'local': ['web']},
               'locked': False}},
    ('multi', 'reject_major gone host'): {'steps': [{'do': 'major local web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap reject_major:gone/web',
                'out': ['<toast> Skipped major update for web.',
                        '<unbutton>',
                        '❌ Unknown host `gone`. Managed hosts: `local`, `nas`, `dead`, '
                        '`refused`, `flaky`'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {'local': ['web']},
               'locked': False}},
    ('single', 'reject_major gone host'): {'steps': [{'do': 'major local web',
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap reject_major:gone/web',
                'out': ['<toast> Skipped major update for web.',
                        '<unbutton>',
                        '❌ Unknown host `gone`. Managed hosts: `local`'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {'local': ['web']},
               'locked': False}},
    # ── lifecycle ───────────────────────────────────────────────────
    # :3520 local only; a bad split or action is a toast.
    ('multi', 'lifecycle:stop:web'): {'steps': [{'do': 'tap lifecycle:stop:web',
                'out': ['<unbutton>', '<toast> Running stop…', '🟥 `web` stopped.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10, inspect --format t=5, stop --time t=90, '
                                   'inspect --format t=5'},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'lifecycle:stop:web'): {'steps': [{'do': 'tap lifecycle:stop:web',
                'out': ['<unbutton>', '<toast> Running stop…', '🟥 `web` stopped.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10, inspect --format t=5, stop --time t=90, '
                                   'inspect --format t=5'},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'lifecycle:restart:web'): {'steps': [{'do': 'tap lifecycle:restart:web',
                'out': ['<unbutton>', '<toast> Running restart…', '🔁 `web` restarted.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120'},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'lifecycle:restart:web'): {'steps': [{'do': 'tap lifecycle:restart:web',
                'out': ['<unbutton>', '<toast> Running restart…', '🔁 `web` restarted.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120'},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'lifecycle:bad:web'): {'steps': [{'do': 'tap lifecycle:bad:web',
                'out': ['<toast> Unknown action'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'lifecycle:bad:web'): {'steps': [{'do': 'tap lifecycle:bad:web',
                'out': ['<toast> Unknown action'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'lifecycle:stop'): {'steps': [{'do': 'tap lifecycle:stop',
                'out': ['<toast> Bad action'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'lifecycle:stop'): {'steps': [{'do': 'tap lifecycle:stop',
                'out': ['<toast> Bad action'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    # ── restart_deps ────────────────────────────────────────────────
    # :3548 local group, needs two members; the cascade runs on a thread.
    ('multi', 'restart_deps:<gid>'): {'steps': [{'do': "group local g1 ['web', 'db']",
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap restart_deps:g1',
                'out': ['<toast> Restarting dependents…',
                        '<unbutton>',
                        '🔁 `web` dependents restarted: `db`'],
                'hosts': {'local': 'inspect --format t=10, restart t=30'},
                'events': ['wait_healthy@local:web max=5'],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'restart_deps:<gid>'): {'steps': [{'do': "group local g1 ['web', 'db']",
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap restart_deps:g1',
                'out': ['<toast> Restarting dependents…',
                        '<unbutton>',
                        '🔁 `web` dependents restarted: `db`'],
                'hosts': {'local': 'inspect --format t=10, restart t=30'},
                'events': ['wait_healthy@local:web max=5'],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'restart_deps one member'): {'steps': [{'do': "group local g1 ['web']",
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap restart_deps:g1',
                'out': ['<toast> No dependents'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'restart_deps one member'): {'steps': [{'do': "group local g1 ['web']",
                'out': [],
                'hosts': {},
                'events': [],
                'raised': None},
               {'do': 'tap restart_deps:g1',
                'out': ['<toast> No dependents'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('multi', 'restart_deps unknown'): {'steps': [{'do': 'tap restart_deps:nosuch',
                'out': ['<toast> No dependents'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'restart_deps unknown'): {'steps': [{'do': 'tap restart_deps:nosuch',
                'out': ['<toast> No dependents'],
                'hosts': {},
                'events': [],
                'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    # ── noop ────────────────────────────────────────────────────────
    # no branch: nothing answers.
    ('multi', 'noop'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5',
                          'nas': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  [✅ web (?)→noop] [🔄 web @nas '
                        '(?)→update_one:nas/web] [🚀 Update all→update_all:1 | ✋ '
                        'Manual→update_skip]',
                        '⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web'],
                'raised': None},
               {'do': 'tap noop', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'noop'): {'steps': [{'do': 'notify None',
                'out': ['🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 ? | 🗓️ Current: ?  [🔄 web (?)→update_one:web] [🚀 Update '
                        'all→update_all:1 | ✋ Manual→update_skip]'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': [],
                'raised': None},
               {'do': 'tap update_one:web',
                'out': ['<toast> Update web...',
                        '<api> editMessageReplyMarkup  [✅ web (?)→noop]',
                        '⏳ Updating `web`...',
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '✅ All updates completed.'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web'],
                'raised': None},
               {'do': 'tap noop', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': [],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    # ── unknown data ────────────────────────────────────────────────
    # no branch: nothing answers.
    ('multi', 'unknown data'): {'steps': [{'do': 'tap bogus', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
    ('single', 'unknown data'): {'steps': [{'do': 'tap bogus', 'out': [], 'hosts': {}, 'events': [], 'raised': None}],
     'state': {'pending': ['web@local'],
               'queue': [],
               'stops': 0,
               'restores': 0,
               'majors': {},
               'locked': False}},
}
# ── end of the generated table ──

if __name__ == "__main__":
    sys.exit(main())
