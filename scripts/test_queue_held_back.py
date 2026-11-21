#!/usr/bin/env python3
"""A pin or an exclusion holds for a queued update too (Q16f).

Since Q13–Q16 an update that finds the lock busy is queued, on every
connection, and the engine's drainer runs it later through `run_keys`.
Neither of them looked at pins. Measured on ffbafc5 through the real
`_handle_callback`, `_on_event` and `do_POST` (scratchpad
q16f/probe.py, probe_dw.py): a container pinned while it waited was
updated by the drain; one pinned before the tap was queued and then
updated; the same for `EXCLUDE_CONTAINERS` and for a `docksentry.pin`
or `docksentry.exclude` label added meanwhile. Before Q16 nothing ran
at all ("an update is already running"), so the queue opened a way past
the pin that the free path does not have for a typed `/update` (its
`check_all` skips a pinned container).

Two places now ask, in the engine, so every connection gets it:

  * putting a key on the queue (`claim_or_queue`, `claim_or_queue_many`):
    a pinned or excluded container is not queued, and the answer says
    so. Only what can be read without asking the host — the pin list
    and `EXCLUDE_CONTAINERS`. A busy request asks no host (Q16), and a
    label only changes with a recreate;
  * right before the drainer runs an entry (`_drain_one`): pins, the
    exclusion setting and the container's labels, read from its host.
    A held-back entry is taken off the queue, said to every channel,
    and stays in the pending list.

An unreadable pin list fails closed in both places (as in
2.19.1-beta.1). At the drain that was already so on ffbafc5 (measured:
the refusal and no update); the check here pins it.

Taken along from the same review: a busy "Update all" tap whose batch
does not fit sends the message's buttons back with "queue full", and its
toast says what happened ("queued" / "not started") instead of "Updates
are starting..." before anything was decided.

Driven through the three matrix harnesses (T3, T4, T5), with steps that
change only the environment — the pin file, the setting, a label on the
stand-in daemon — never the decision.
"""

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import test_telegram_dispatch_matrix as m  # noqa: E402
import test_telegram_callback_matrix as t3  # noqa: E402
import test_discord_dispatch_matrix as t4  # noqa: E402
import test_web_action_matrix as t5  # noqa: E402

# ── the sentences, written by hand before the change ───────────────────
PIN_NQ = "📌 `{}` is pinned — not queued. Unpin it if it should be updated."
EXCL_NQ = "🚫 `{}` is excluded from updates — not queued."
LINE_PIN = "• `{}` — pinned, not queued"
LINE_EXCL = "• `{}` — excluded from updates, not queued"
SKIP_PIN = ("📌 `{}` was pinned while it waited in the queue — its update "
            "did not run.")
SKIP_EXCL = ("🚫 `{}` is excluded from updates — its queued update did not "
             "run.")
REFUSED = ("📌 No update was started: the pin list (`pinned_containers.json`)"
           " could not be read, so every container counts as pinned. Repair "
           "or remove the file, then try again.")
HEAD = ("🕒 Another update is running right now. These wait in the queue "
        "and run when their turn comes:")
TOAST_START = "<toast> Updates are starting..."
TOAST_QUEUED = "<toast> Queued — details in the chat."
TOAST_NOT = "<toast> Not started — details in the chat."


# ── extra environment steps, laid over each harness's own ──────────────
def _env_step(bot, cfg, backends, step):
    """True when `step` was one of the steps this file adds."""
    kind, args = step[0], step[1:]
    if kind == "pin":
        bot._store_for(args[0]).pin(args[1])
    elif kind == "raw_pins":
        with open(cfg.pinned_file, "wb") as f:
            f.write(args[0])
    elif kind == "exclude":
        cfg.exclude_containers = list(args[0])
    elif kind == "label":
        backends[args[0]].containers[args[1]]["Config"]["Labels"][
            args[2]] = args[3]
    else:
        return False
    return True


_t3_do = t3.Row.do


def _t3_step(self, step):
    if not _env_step(self.bot, self.bot.config, self.backends, step):
        return _t3_do(self, step)


t3.Row.do = _t3_step

_t4_do = t4.Row.do


def _t4_step(self, step):
    if not _env_step(self.bot, self.cfg, self.backends, step):
        return _t4_do(self, step)


t4.Row.do = _t4_step

_t5_do = t5.Row.do


def _t5_step(self, step):
    if not _env_step(self.bot, self.cfg, self.backends, step):
        return _t5_do(self, step)
    return None


t5.Row.do = _t5_step

SINGLE = ("none", "one")


def _ran(st):
    return [e for e in st.get("events", ())
            if e.startswith("update_container@")]


def _pending(rec):
    return rec["state"].get("pending") or []


# ── Telegram (T3) ──────────────────────────────────────────────────────
def telegram_checks():
    out = []

    def row(setup, steps):
        rec, esc = t3.run_scenario(setup, steps)
        return rec, rec["steps"], esc

    for setup in m.SETUPS:
        # Pinned while it waited: a tap.
        rec, st, esc = row(setup, [("lock",), ("tap", "update_one:web"),
                                   ("pin", "local", "web"), ("release",)])
        rel = st[-1]
        out.append((f"[{setup}] tap queued, then pinned: the drain does not "
                    f"update it", _ran(rel) == [] and not esc,
                    f"events {rel['events']!r}, escapes {esc!r}"))
        out.append((f"[{setup}] tap queued, then pinned: it says so",
                    SKIP_PIN.format("web") in rel["out"],
                    f"out {rel['out']!r}"))
        out.append((f"[{setup}] tap queued, then pinned: off the queue, "
                    f"still pending, lock free",
                    rec["state"]["queue"] == []
                    and "web@local" in _pending(rec)
                    and not rec["state"]["locked"],
                    f"state {rec['state']!r}"))
        # The other direction: nothing pinned, the drain runs it.
        rec, st, esc = row(setup, [("lock",), ("tap", "update_one:web"),
                                   ("pin", "local", "db"), ("release",)])
        out.append((f"[{setup}] tap queued, another container pinned: the "
                    f"drain updates it",
                    _ran(st[-1]) == ["update_container@local:web"]
                    and not any("pinned" in ln for ln in st[-1]["out"]),
                    f"events {st[-1]['events']!r}, out {st[-1]['out']!r}"))
        # Pinned before the tap: not queued, and said.
        rec, st, esc = row(setup, [("pin", "local", "web"), ("lock",),
                                   ("tap", "update_one:web"), ("release",)])
        tap = st[2]
        out.append((f"[{setup}] pinned, then tapped while busy: not queued, "
                    f"and the answer says pinned",
                    tap["out"][-1:] == [PIN_NQ.format("web")]
                    and rec["state"]["queue"] == [] and _ran(st[-1]) == []
                    and tap["hosts"] == {},
                    f"tap {tap['out']!r} hosts {tap['hosts']!r}, release "
                    f"{st[-1]['events']!r}, queue {rec['state']['queue']!r}"))
        # Typed /update, pinned before.
        rec, st, esc = row(setup, [("pin", "local", "web"), ("lock",),
                                   ("type", "/update web"), ("unlock",)])
        out.append((f"[{setup}] pinned, then /update web while busy: not "
                    f"queued, says pinned",
                    st[2]["out"][-1:] == [PIN_NQ.format("web")]
                    and rec["state"]["queue"] == [],
                    f"out {st[2]['out']!r}, queue {rec['state']['queue']!r}"))
        # EXCLUDE_CONTAINERS, before and after.
        rec, st, esc = row(setup, [("exclude", ["we*"]), ("lock",),
                                   ("tap", "update_one:web"), ("unlock",)])
        out.append((f"[{setup}] excluded, then tapped while busy: not "
                    f"queued, says excluded",
                    st[2]["out"][-1:] == [EXCL_NQ.format("web")]
                    and rec["state"]["queue"] == [],
                    f"out {st[2]['out']!r}, queue {rec['state']['queue']!r}"))
        rec, st, esc = row(setup, [("lock",), ("tap", "update_one:web"),
                                   ("exclude", ["we*"]), ("release",)])
        out.append((f"[{setup}] tap queued, then excluded: not updated, "
                    f"says excluded, still pending",
                    _ran(st[-1]) == []
                    and SKIP_EXCL.format("web") in st[-1]["out"]
                    and rec["state"]["queue"] == []
                    and "web@local" in _pending(rec),
                    f"out {st[-1]['out']!r}, events {st[-1]['events']!r}"))
        # Labels, read from the host at the drain.
        for label, value, want in (("docksentry.pin", "true", SKIP_PIN),
                                   ("docksentry.exclude", "true", SKIP_EXCL),
                                   ("docksentry.enable", "false", SKIP_EXCL)):
            rec, st, esc = row(setup, [
                ("lock",), ("tap", "update_one:web"),
                ("label", "local", "web", label, value), ("release",)])
            out.append((f"[{setup}] tap queued, then {label}={value}: not "
                        f"updated, said",
                        _ran(st[-1]) == [] and want.format("web")
                        in st[-1]["out"] and "web@local" in _pending(rec),
                        f"out {st[-1]['out']!r}, events "
                        f"{st[-1]['events']!r}"))
        # An unreadable pin list during the drain: refused, nothing runs,
        # the entry stays pending (already so before Q16f — pinned here).
        rec, st, esc = row(setup, [("lock",), ("tap", "update_one:web"),
                                   ("raw_pins", b'["db"'), ("release",)])
        out.append((f"[{setup}] tap queued, then the pin list unreadable: "
                    f"refused, nothing updated, still pending",
                    _ran(st[-1]) == [] and REFUSED in st[-1]["out"]
                    and rec["state"]["queue"] == []
                    and "web@local" in _pending(rec) and not esc,
                    f"out {st[-1]['out']!r}, events {st[-1]['events']!r}"))

    # "Update all" on two hosts: nas's web pinned while the batch waited.
    rec, st, esc = row("multi", [("notify", None), ("lock",),
                                 ("tap", "update_all:<tok>"),
                                 ("pin", "nas", "web"), ("release",)])
    rel = st[-1]
    out.append(("[multi] Update all queued, then web@nas pinned: only the "
                "local web runs, nas's is said and stays pending",
                _ran(rel) == ["update_container@local:web"]
                and SKIP_PIN.format("web @nas") in rel["out"]
                and _pending(rec) == ["web@nas"]
                and rec["state"]["queue"] == [],
                f"out {rel['out']!r}, events {rel['events']!r}, "
                f"state {rec['state']!r}"))
    for setup in SINGLE:
        rec, st, esc = row(setup, [("notify", None), ("lock",),
                                   ("tap", "update_all:<tok>"),
                                   ("pin", "local", "web"), ("release",)])
        out.append((f"[{setup}] Update all queued, then pinned: not updated",
                    _ran(st[-1]) == []
                    and SKIP_PIN.format("web") in st[-1]["out"]
                    and _pending(rec) == ["web@local"],
                    f"out {st[-1]['out']!r}, events {st[-1]['events']!r}"))
    # Pinned before a busy "Update all" on two hosts: the rest is queued,
    # the pinned one is named.
    rec, st, esc = row("multi", [("notify", None), ("pin", "nas", "web"),
                                 ("lock",), ("tap", "update_all:<tok>"),
                                 ("release",)])
    want = "\n".join([HEAD, "• `web` — position 1",
                      LINE_PIN.format("web @nas")])
    out.append(("[multi] web@nas pinned, then Update all while busy: web "
                "queued, web@nas named as pinned and not queued",
                st[3]["out"][-1:] == [want]
                and _ran(st[-1]) == ["update_container@local:web"],
                f"out {st[3]['out']!r}, release {st[-1]['events']!r}"))

    # Both pinned: nothing waits, so no "these wait in the queue"
    # heading over two lines that say the opposite.
    rec, st, esc = row("multi", [("notify", None), ("pin", "local", "web"),
                                 ("pin", "nas", "web"), ("lock",),
                                 ("tap", "update_all:<tok>"), ("unlock",)])
    want = "\n".join([LINE_PIN.format("web"), LINE_PIN.format("web @nas")])
    out.append(("[multi] both pinned, then Update all while busy: two lines, "
                "no queue heading, nothing queued, toast not started",
                st[4]["out"][-1:] == [want] and TOAST_NOT in st[4]["out"]
                and rec["state"]["queue"] == [],
                f"out {st[4]['out']!r}, queue {rec['state']['queue']!r}"))

    # The toast says what happened, the buttons come back under "queue
    # full".
    from update_engine import UpdateEngine
    mx = UpdateEngine.UPDATE_QUEUE_MAX
    for setup in m.SETUPS:
        rec, st, esc = row(setup, [("notify", None), ("tap",
                                                      "update_all:<tok>")])
        out.append((f"[{setup}] Update all, lock free: the toast says "
                    f"starting", TOAST_START in st[1]["out"],
                    f"out {st[1]['out']!r}"))
        rec, st, esc = row(setup, [("notify", None), ("lock",),
                                   ("tap", "update_all:<tok>"), ("unlock",)])
        out.append((f"[{setup}] Update all, lock held: the toast says queued",
                    TOAST_QUEUED in st[2]["out"]
                    and TOAST_START not in st[2]["out"],
                    f"out {st[2]['out']!r}"))
        rec, st, esc = row(setup, [("drop_pending",), ("lock",),
                                   ("tap", "update_all"), ("unlock",)])
        out.append((f"[{setup}] Update all, lock held, nothing pending: the "
                    f"toast says not started",
                    TOAST_NOT in st[2]["out"]
                    and TOAST_START not in st[2]["out"],
                    f"out {st[2]['out']!r}"))
        free = 0 if setup in SINGLE else 1
        rec, st, esc = row(setup, [("notify", None), ("lock",),
                                   ("fill", mx - free),
                                   ("tap", "update_all:<tok>"), ("unlock",)])
        offered = st[0]["out"][-1]
        tap = st[3]["out"]
        kb = (offered[offered.index("  [") + 2:] if "  [" in offered
              else "<none offered>")
        out.append((f"[{setup}] Update all, queue full: not started, and "
                    f"the answer carries the notification's buttons again",
                    TOAST_NOT in tap and TOAST_START not in tap
                    and bool(tap) and tap[-1].endswith("  " + kb)
                    and "full" in tap[-1],
                    f"out {tap!r}, offered {kb!r}"))
    return out


# ── Discord (T4) ───────────────────────────────────────────────────────
def discord_checks():
    out = []
    for setup in t4.SETUPS:
        rec, esc = t4.run_scenario(setup, [
            ("pin", "local", "web"), ("lock",),
            t4._c("update", ("container", "web")), ("release",)])
        st = rec["steps"]
        said = "\n".join(st[2].get("out", []))
        out.append((f"[{setup}] Discord: pinned, then /update web while "
                    f"busy: not queued, says pinned",
                    PIN_NQ.format("web") in said and "queued — position"
                    not in said and not rec["state"].get("queued")
                    and _ran(st[3]) == [],
                    f"out {st[2].get('out')!r}, release {st[3]!r}"))
        rec, esc = t4.run_scenario(setup, [
            ("lock",), t4._c("update", ("container", "web")),
            ("pin", "local", "web"), ("release",)])
        rel = rec["steps"][-1]
        said = "\n".join(rel.get("out", []))
        out.append((f"[{setup}] Discord: /update web queued, then pinned: "
                    f"not updated, said",
                    _ran(rel) == [] and SKIP_PIN.format("web") in said
                    and "web@local" in _pending(rec),
                    f"out {rel.get('out')!r}, events {rel.get('events')!r}"))
        rec, esc = t4.run_scenario(setup, [
            ("lock",), t4._c("update", ("container", "web")), ("release",)])
        out.append((f"[{setup}] Discord: /update web queued, nothing pinned: "
                    f"the drain updates it",
                    _ran(rec["steps"][-1]) == ["update_container@local:web"],
                    f"release {rec['steps'][-1]!r}"))
    return out


# ── Web (T5) ───────────────────────────────────────────────────────────
def web_checks():
    out = []
    P = t5.P
    rec, esc = t5.run_scenario("multi", [
        ("pin", "local", "web"), ("lock",),
        P("/api/update", ("name", "web")), ("release",)])
    st = rec["steps"]
    out.append(("[multi] Web: pinned, then Update while busy: not queued, "
                "the page says held back",
                st[2].get("http") == "303 | Location: /?update=held&n=1"
                and not rec["state"].get("queued") and _ran(st[3]) == [],
                f"http {st[2].get('http')!r}, release {st[3]!r}"))
    rec, esc = t5.run_scenario("multi", [
        ("pin", "nas", "web"), ("lock",),
        P("/api/bulk", ("action", "update"), ("names", "web,nas/web")),
        ("release",)])
    st = rec["steps"]
    out.append(("[multi] Web: bulk web,nas/web with nas's pinned, busy: web "
                "queued, one held back, only web runs",
                st[2].get("http")
                == "303 | Location: /?update=queued&pos=1&held=1"
                and _ran(st[3]) == ["update_container@local:web"],
                f"http {st[2].get('http')!r}, release {st[3]!r}"))
    rec, esc = t5.run_scenario("multi", [
        ("lock",), P("/api/update", ("name", "web")),
        ("pin", "local", "web"), ("release",)])
    rel = rec["steps"][-1]
    said = "\n".join(rel.get("out", []))
    # An unreadable pin list at the claim (the handlers ask first; this
    # is the race) is the page's existing "pin list" answer.
    row = t5.Row("multi")
    try:
        to = row.handler._queue_redirect(
            None, [("web", "update_refused_pin_list", {}),
                   ("nas/web", "update_refused_pin_list", {})])
    finally:
        shutil.rmtree(row.data, ignore_errors=True)
    out.append(("Web: a request refused for an unreadable pin list goes to "
                "the pin-list banner", to == "/?update=pinlist", f"to {to!r}"))
    out.append(("[multi] Web: Update queued, then pinned: not updated, said",
                _ran(rel) == [] and SKIP_PIN.format("web") in said
                and "web@local" in _pending(rec),
                f"out {rel.get('out')!r}, events {rel.get('events')!r}"))
    return out


# ── the engine, asked directly ─────────────────────────────────────────
def engine_checks():
    """An unreadable pin list at the moment of queueing: every
    connection asks first and refuses, so this is the race between that
    question and the claim. The engine refuses the whole request and
    queues nothing."""
    import contextlib
    import io
    out = []
    row = t3.Row("multi")
    # The unreadable file's two ERROR lines go to the log, not here.
    with contextlib.redirect_stdout(io.StringIO()):
        _engine_checks(row, out)
    return out


def _engine_checks(row, out):
    try:
        eng = row.bot.engine
        with open(row.bot.config.pinned_file, "wb") as f:
            f.write(b'["db"')
        eng._update_lock.acquire()
        try:
            # `gone` is a host the registry does not know: nobody asks its
            # pin list, and it must not slip onto the queue either.
            claimed, outcomes = eng.claim_or_queue_many(
                ["web", "nas/web", "gone/web"])
            one = eng.claim_or_queue("web")
        finally:
            eng._update_lock.release()
        out.append(("engine: unreadable pin list while busy — a batch is "
                    "refused as a whole, nothing queued",
                    not claimed and [a for _k, a, _p in outcomes]
                    == ["update_refused_pin_list"] * 3
                    and eng.queued_updates() == [],
                    f"outcomes {outcomes!r}, queue {eng.queued_updates()!r}"))
        out.append(("Telegram: a batch refused that way gets the refusal "
                    "once", row.bot._queued_answer(outcomes) == REFUSED,
                    f"said {row.bot._queued_answer(outcomes)!r}"))
        out.append(("engine: …and so is a single tap",
                    one[:2] == (False, "update_refused_pin_list")
                    and eng.queued_updates() == [],
                    f"answer {one!r}"))
        # The other direction, on the same engine: a readable list queues.
        eng.drop_queued_updates()
        with open(row.bot.config.pinned_file, "w") as f:
            f.write("[]")
        eng._update_lock.acquire()
        try:
            claimed, outcomes = eng.claim_or_queue_many(["web", "nas/web"])
        finally:
            eng._update_lock.release()
        out.append(("engine: a readable, empty pin list queues both",
                    [a for _k, a, _p in outcomes] == ["update_queued"] * 2,
                    f"outcomes {outcomes!r}"))
    finally:
        shutil.rmtree(row.data, ignore_errors=True)


def main():
    m._TMP_ROOT = tempfile.mkdtemp(prefix="queue-held-back-")
    failed = 0
    try:
        for part in (telegram_checks, discord_checks, web_checks,
                     engine_checks):
            for label, ok, detail in part():
                print(f"  {'PASS' if ok else 'FAIL'}  {label}")
                if not ok:
                    print(f"      → {detail}")
                    failed += 1
    finally:
        shutil.rmtree(m._TMP_ROOT, ignore_errors=True)
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
