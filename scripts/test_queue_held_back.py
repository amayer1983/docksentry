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

The free path, too (Q16g). A pending entry passed the pins when it was
found, and pinning a container does not touch the pending list. So the
entry outlives the pin, and every manual way that acts on the pending
list ran past it on a free lock: measured on d5efbc9 for `/updateall`
(scratchpad q16rev/probe2.py), and on main (0779842, 2.19.1-beta.1) for
the tap, "Update all" with and without a snapshot, the major
confirmation, Discord `/update` and `/updateall`, and the Web UI row and
bulk buttons (scratchpad q16g/probe/probe_main.py). Only a typed
`/update <name|glob>` held, because it checks first and the check skips
a pinned container. Now the batch every one of them runs through asks
`held_back` (labels included) right before `update_container`, and the
major confirmation asks right before its own. A held container is not
updated, its line says why, and its pending entry (or deferred major)
stays. Rows where the pin lands while the batch is already running,
during a group's pause, pin "right before".

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
SKIP_PIN_LABEL = ("📌 `{}` has a `docksentry.pin` label — its queued update "
                  "did not run.")
SKIP_EXCL = ("🚫 `{}` is excluded from updates — its queued update did not "
             "run.")
REFUSED = ("📌 No update was started: the pin list (`pinned_containers.json`)"
           " could not be read, so every container counts as pinned. Repair "
           "or remove the file, then try again.")
#: The drain's refusal names the entry (Q16i); it was the nameless
#: REFUSED once per entry before.
SKIP_PINLIST = ("📌 `{}` did not run from the queue: the pin list "
                "(`pinned_containers.json`) could not be read, so every "
                "container counts as pinned. Repair or remove the file, then "
                "start it again.")
HEAD = ("🕒 Another update is running right now. These wait in the queue "
        "and run when their turn comes:")
TOAST_START = "<toast> Updates are starting..."
TOAST_QUEUED = "<toast> Queued — details in the chat."
TOAST_NOT = "<toast> Not started — details in the chat."
# The free path (Q16g): one line per container, in the shape of the
# batch's result lines, so `{}` is the name as that line writes it.
HELD_PIN = "📌 {}: pinned — not updated."
HELD_PIN_LABEL = "📌 {}: has a `docksentry.pin` label — not updated."
HELD_EXCL = "🚫 {}: excluded from updates — not updated."
HELD_PINLIST = ("📌 {}: not updated — the pin list (`pinned_containers.json`)"
                " could not be read, so every container counts as pinned.")
#: How the batch's lines name `web` here (the stand-in resolves a link).
LWEB = "[web](https://github.com/example/web/releases/latest)"
NWEB = LWEB + " @nas"


# ── extra environment steps, laid over each harness's own ──────────────
def _env_step(bot, cfg, backends, step):
    """True when `step` was one of the steps this file adds."""
    kind, args = step[0], step[1:]
    if kind == "pin":
        bot._store_for(args[0]).pin(args[1])
    elif kind == "unpin":
        bot._store_for(args[0]).unpin(args[1])
    elif kind == "raw_pins":
        with open(cfg.pinned_file, "wb") as f:
            f.write(args[0])
    elif kind == "exclude":
        cfg.exclude_containers = list(args[0])
    elif kind == "label":
        backends[args[0]].containers[args[1]]["Config"]["Labels"][
            args[2]] = args[3]
    elif kind == "on_wait":
        # The next time the engine waits (a group's pause, a cooldown),
        # `args[0]` happens: a pin set while a batch is already running.
        eng = bot.engine
        before, todo = eng._sleep, [args[0]]

        def sleep(seconds):
            before(seconds)
            if todo:
                _env_step(bot, cfg, backends, todo.pop())
        eng._sleep = sleep
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
        # Queued, pinned, tapped again: the answer says "not queued", so
        # it must not be on the queue any more. Before the fix it still
        # was, and unpinning it let the drain update it with no new tap
        # (measured on 8556e4f, review of Q16f).
        for hold, free, name, said in (
                (("pin", "local", "web"), ("unpin", "local", "web"),
                 "pinned", PIN_NQ),
                (("exclude", ["we*"]), ("exclude", []), "excluded",
                 EXCL_NQ)):
            rec, st, esc = row(setup, [("lock",), ("tap", "update_one:web"),
                                       hold, ("tap", "update_one:web"),
                                       free, ("release",)])
            out.append((f"[{setup}] tap queued, then {name}, tapped again, "
                        f"then no longer {name}: the second answer says not "
                        f"queued, and the drain updates nothing",
                        st[3]["out"][-1:] == [said.format("web")]
                        and _ran(st[-1]) == [] and not esc
                        and rec["state"]["queue"] == []
                        and "web@local" in _pending(rec),
                        f"second tap {st[3]['out']!r}, release "
                        f"{st[-1]['events']!r}, state {rec['state']!r}"))
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
        for label, value, want in (("docksentry.pin", "true", SKIP_PIN_LABEL),
                                   ("docksentry.exclude", "true", SKIP_EXCL),
                                   ("docksentry.enable", "false", SKIP_EXCL)):
            rec, st, esc = row(setup, [
                ("lock",), ("tap", "update_one:web"),
                ("label", "local", "web", label, value), ("release",)])
            out.append((f"[{setup}] tap queued, then {label}={value}: not "
                        f"updated, said",
                        _ran(st[-1]) == [] and want.format("web")
                        in st[-1]["out"] and "web@local" in _pending(rec)
                        and SKIP_PIN.format("web") not in st[-1]["out"],
                        f"out {st[-1]['out']!r}, events "
                        f"{st[-1]['events']!r}"))
        # A label that was there before the tap: queueing asks no host
        # (Q16), so it is queued and held at the drain. "Was pinned while
        # it waited" would be false there; the line names the label.
        rec, st, esc = row(setup, [
            ("label", "local", "web", "docksentry.pin", "true"),
            ("lock",), ("tap", "update_one:web"), ("release",)])
        out.append((f"[{setup}] docksentry.pin label before the tap: queued, "
                    f"the drain names the label, not a pin set meanwhile",
                    _ran(st[-1]) == []
                    and SKIP_PIN_LABEL.format("web") in st[-1]["out"]
                    and SKIP_PIN.format("web") not in st[-1]["out"]
                    and "web@local" in _pending(rec),
                    f"out {st[-1]['out']!r}, events {st[-1]['events']!r}"))
        # Held by the drain's own question, before the wait and before
        # "Updating …": a queued entry pinned or labelled since is not
        # announced as running first (Q16f). The batch would hold it too
        # (Q16g), but only after that line.
        for env, name in ((("pin", "local", "web"), "pinned"),
                          (("label", "local", "web", "docksentry.pin",
                            "true"), "a docksentry.pin label")):
            rec, st, esc = row(setup, [("lock",), ("tap", "update_one:web"),
                                       env, ("release",)])
            out.append((f"[{setup}] tap queued, then {name}: the drain "
                        f"does not say it is updating it",
                        _ran(st[-1]) == [] and not any(
                            o.startswith("⏳ Updating `web`")
                            for o in st[-1]["out"]),
                        f"out {st[-1]['out']!r}"))
        # An unreadable pin list during the drain: refused, nothing runs,
        # the entry stays pending (already so before Q16f — pinned here).
        # The refusal names the entry (Q16i).
        rec, st, esc = row(setup, [("lock",), ("tap", "update_one:web"),
                                   ("raw_pins", b'["db"'), ("release",)])
        out.append((f"[{setup}] tap queued, then the pin list unreadable: "
                    f"refused by name, nothing updated, still pending",
                    _ran(st[-1]) == []
                    and SKIP_PINLIST.format("web") in st[-1]["out"]
                    and REFUSED not in st[-1]["out"]
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
    # Update all queued, web@nas pinned, Update all tapped again: web@nas
    # is named as not queued, so unpinning it must not let the drain run
    # it. The local web keeps its place.
    rec, st, esc = row("multi", [("notify", None), ("lock",),
                                 ("tap", "update_all:<tok>"),
                                 ("pin", "nas", "web"),
                                 ("tap", "update_all:<tok>"),
                                 ("unpin", "nas", "web"), ("release",)])
    out.append(("[multi] Update all queued, web@nas pinned, Update all "
                "again, then unpinned: web@nas named as not queued, only "
                "the local web runs",
                bool(st[4]["out"])
                and LINE_PIN.format("web @nas") in st[4]["out"][-1]
                and _ran(st[-1]) == ["update_container@local:web"]
                and rec["state"]["queue"] == []
                and _pending(rec) == ["web@nas"] and not esc,
                f"second tap {st[4]['out']!r}, release {st[-1]['events']!r},"
                f" state {rec['state']!r}"))
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
    # Several waiting, one held: the count goes on the multi answer.
    rec, esc = t5.run_scenario("multi", [
        ("add_pending", "db", "postgres:16", "local"),
        ("pin", "nas", "web"), ("lock",),
        P("/api/bulk", ("action", "update"),
          ("names", "web,db,nas/web")),
        ("release",)])
    st = rec["steps"][1:]
    out.append(("[multi] Web: bulk web,db,nas/web with nas's pinned, "
                "busy: two queued, the page says one held back",
                st[2].get("http")
                == "303 | Location: /?update=queued&n=2&held=1",
                f"http {st[2].get('http')!r}"))
    # Already queued, one held: the count goes on that answer too. The
    # seconds are the wall clock's, so only the shape is compared.
    rec, esc = t5.run_scenario("multi", [
        ("pin", "nas", "web"), ("lock",),
        P("/api/update", ("name", "web")),
        P("/api/bulk", ("action", "update"), ("names", "web,nas/web")),
        ("release",)])
    st = rec["steps"]
    http = st[3].get("http") or ""
    out.append(("[multi] Web: bulk web,nas/web with web already queued and "
                "nas's pinned: already queued, the page says one held back",
                http.startswith("303 | Location: /?update=already_queued"
                                "&since=")
                and http.endswith("&held=1"),
                f"http {http!r}"))
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


# ── the free path (Q16g) ───────────────────────────────────────────────
def _said(st, line):
    return any(line in o for o in st.get("out", ()))


def telegram_free_checks():
    out = []

    def row(setup, steps):
        rec, esc = t3.run_scenario(setup, steps)
        return rec, rec["steps"], esc

    for setup in m.SETUPS:
        # The tap on a notification from before the pin.
        rec, st, esc = row(setup, [("pin", "local", "web"),
                                   ("tap", "update_one:web")])
        out.append((f"[{setup}] free: pinned, then its button tapped: not "
                    f"updated, says pinned, still pending",
                    _ran(st[-1]) == [] and not esc
                    and _said(st[-1], HELD_PIN.format("`web`"))
                    and "web@local" in _pending(rec)
                    and not rec["state"]["locked"],
                    f"out {st[-1]['out']!r}, events {st[-1]['events']!r}, "
                    f"state {rec['state']!r}, escapes {esc!r}"))
        # The same with EXCLUDE_CONTAINERS and with labels.
        for env, said in (
                (("exclude", ["we*"]), HELD_EXCL.format("`web`")),
                (("label", "local", "web", "docksentry.pin", "true"),
                 HELD_PIN_LABEL.format("`web`")),
                (("label", "local", "web", "docksentry.exclude", "true"),
                 HELD_EXCL.format("`web`")),
                (("label", "local", "web", "docksentry.enable", "false"),
                 HELD_EXCL.format("`web`"))):
            rec, st, esc = row(setup, [env, ("tap", "update_one:web")])
            out.append((f"[{setup}] free: {env[0]} {env[-2:]}, then its "
                        f"button tapped: not updated, said, still pending",
                        _ran(st[-1]) == [] and not esc
                        and _said(st[-1], said)
                        and "web@local" in _pending(rec),
                        f"out {st[-1]['out']!r}, events "
                        f"{st[-1]['events']!r}, escapes {esc!r}"))
        # "Update all" from the notification, `update_all` bare and a
        # typed /updateall: the last two read the pending file and used
        # to clear it whole.
        for name, steps in (
                ("Update all", [("notify", None), ("pin", "local", "web"),
                                ("tap", "update_all:<tok>")]),
                ("bare update_all", [("pin", "local", "web"),
                                     ("tap", "update_all")]),
                # Bare, /updateall means the local host; @all is every one.
                ("/updateall", [("pin", "local", "web"),
                                ("type", "/updateall @all" if setup == "multi"
                                 else "/updateall")])):
            # On two hosts nas's web runs, the local one is held.
            rec, st, esc = row(setup, steps)
            want_ran = ([] if setup in SINGLE
                        else ["update_container@nas:web"])
            want_left = ["web@local"]
            out.append((f"[{setup}] free: web pinned, then {name}: the "
                        f"local web is not updated, said, still pending",
                        _ran(st[-1]) == want_ran and not esc
                        and _said(st[-1], HELD_PIN.format(LWEB))
                        and _pending(rec) == want_left,
                        f"out {st[-1]['out']!r}, events "
                        f"{st[-1]['events']!r}, state {rec['state']!r}, "
                        f"escapes {esc!r}"))
        # The major confirmation runs its own update_container.
        rec, st, esc = row(setup, [("major", "local", "web"),
                                   ("pin", "local", "web"),
                                   ("tap", "confirm_major:web")])
        out.append((f"[{setup}] free: a deferred major, then pinned, then "
                    f"confirmed: not updated, says pinned, the major stays",
                    _ran(st[-1]) == [] and not esc
                    and _said(st[-1], HELD_PIN.format("`web`"))
                    and rec["state"]["majors"] == {"local": ["web"]}
                    and not rec["state"]["locked"],
                    f"out {st[-1]['out']!r}, events {st[-1]['events']!r}, "
                    f"state {rec['state']!r}"))
        # …and a label set since the prompt: the confirmation reads it.
        rec, st, esc = row(setup, [("major", "local", "web"),
                                   ("label", "local", "web",
                                    "docksentry.pin", "true"),
                                   ("tap", "confirm_major:web")])
        out.append((f"[{setup}] free: a deferred major, then a "
                    f"docksentry.pin label, then confirmed: not updated, "
                    f"names the label, the major stays",
                    _ran(st[-1]) == [] and not esc
                    and _said(st[-1], HELD_PIN_LABEL.format("`web`"))
                    and rec["state"]["majors"] == {"local": ["web"]},
                    f"out {st[-1]['out']!r}, events {st[-1]['events']!r}, "
                    f"state {rec['state']!r}"))
        # A typed /update checks first, and the check skips a pinned
        # container: already so before Q16g, kept here as the path the
        # others now match.
        rec, st, esc = row(setup, [("pin", "local", "web"),
                                   ("type", "/update web")])
        out.append((f"[{setup}] free: pinned, then /update web: not updated",
                    _ran(st[-1]) == [] and "web@local" in _pending(rec),
                    f"out {st[-1]['out']!r}, events {st[-1]['events']!r}"))
        # Pinned while the batch already runs: during the group's pause
        # between db and web, after db was recreated.
        rec, st, esc = row(setup, [
            ("add_pending", "db", "postgres:16", "local"),
            ("group", "local", "g1", ["db", "web"]), ("notify", ["local"]),
            ("on_wait", ("pin", "local", "web")),
            ("tap", "update_all:<tok>")])
        out.append((f"[{setup}] free: Update all over db, web; web pinned "
                    f"during the pause after db: db runs, web does not",
                    _ran(st[-1]) == ["update_container@local:db"]
                    and _said(st[-1], HELD_PIN.format(LWEB))
                    and "web@local" in _pending(rec)
                    and "db@local" not in _pending(rec) and not esc,
                    f"out {st[-1]['out']!r}, events {st[-1]['events']!r}, "
                    f"state {rec['state']!r}"))
        # A tap that waits for the group's pause after the tap before.
        for env, said, what in (
                (("pin", "local", "web"), HELD_PIN.format("`web`"),
                 "pinned"),
                (("raw_pins", b'["db"'), HELD_PINLIST.format("`web`"),
                 "the pin list unreadable")):
            rec, st, esc = row(setup, [
                ("add_pending", "db", "postgres:16", "local"),
                ("group", "local", "g1", ["db", "web"]),
                ("tap", "update_one:db"), ("on_wait", env),
                ("tap", "update_one:web")])
            out.append((f"[{setup}] free: db tapped, then web; {what} "
                        f"during web's wait for the group's pause: web is "
                        f"not updated, said, still pending",
                        _ran(st[-1]) == [] and not esc
                        and any(e.startswith("wait ")
                                for e in st[-1]["events"])
                        and _said(st[-1], said)
                        and "web@local" in _pending(rec),
                        f"out {st[-1]['out']!r}, events "
                        f"{st[-1]['events']!r}, state {rec['state']!r}"))
        # The drain: web queued behind db, pinned during its wait. The
        # drain's own question before the wait found nothing.
        rec, st, esc = row(setup, [
            ("add_pending", "db", "postgres:16", "local"),
            ("group", "local", "g1", ["db", "web"]), ("lock",),
            ("tap", "update_one:db"), ("tap", "update_one:web"),
            ("on_wait", ("pin", "local", "web")), ("release",)])
        out.append((f"[{setup}] queued db, web; web pinned during its wait "
                    f"in the drain: db runs, web does not, said",
                    _ran(st[-1]) == ["update_container@local:db"]
                    and _said(st[-1], SKIP_PIN.format("web"))
                    and "web@local" in _pending(rec)
                    and rec["state"]["queue"] == [] and not esc,
                    f"out {st[-1]['out']!r}, events {st[-1]['events']!r}, "
                    f"state {rec['state']!r}"))
        # …and the pin list broken there: refused, fail closed.
        rec, st, esc = row(setup, [
            ("add_pending", "db", "postgres:16", "local"),
            ("group", "local", "g1", ["db", "web"]), ("lock",),
            ("tap", "update_one:db"), ("tap", "update_one:web"),
            ("on_wait", ("raw_pins", b'["db"')), ("release",)])
        out.append((f"[{setup}] queued db, web; the pin list unreadable "
                    f"during web's wait in the drain: web does not run, "
                    f"the refusal is said by name, still pending",
                    _ran(st[-1]) == ["update_container@local:db"]
                    and SKIP_PINLIST.format("web") in st[-1]["out"]
                    and REFUSED not in st[-1]["out"]
                    and "web@local" in _pending(rec) and not esc,
                    f"out {st[-1]['out']!r}, events {st[-1]['events']!r}, "
                    f"state {rec['state']!r}"))

    # Two hosts, nas's web pinned: the local one runs, nas's is named.
    for name, steps in (
            ("its button", [("pin", "nas", "web"),
                            ("tap", "update_one:nas/web")]),
            ("Update all", [("notify", None), ("pin", "nas", "web"),
                            ("tap", "update_all:<tok>")]),
            ("/updateall @all", [("pin", "nas", "web"),
                                 ("type", "/updateall @all")])):
        rec, st, esc = row("multi", steps)
        one = name == "its button"
        out.append((f"[multi] free: web@nas pinned, then {name}: web@nas is "
                    f"not updated, said, still pending",
                    _ran(st[-1]) == ([] if one
                                     else ["update_container@local:web"])
                    and _said(st[-1], HELD_PIN.format(
                        "`web @nas`" if one else NWEB))
                    and _pending(rec) == (["web@local", "web@nas"] if one
                                          else ["web@nas"]) and not esc,
                    f"out {st[-1]['out']!r}, events {st[-1]['events']!r}, "
                    f"state {rec['state']!r}"))
    return out


def discord_free_checks():
    out = []
    for setup in t4.SETUPS:
        for name, steps in (
                ("/update web", [t4._c("update", ("container", "web"))]),
                ("/updateall and the press", [t4._c("updateall"),
                                              ("press", "updateall")])):
            for env, said in ((("pin", "local", "web"),
                               HELD_PIN.format(LWEB)),
                              (("exclude", ["we*"]),
                               HELD_EXCL.format(LWEB))):
                rec, esc = t4.run_scenario(setup, [env] + steps)
                last = rec["steps"][-1]
                left = _pending(rec)
                out.append((f"[{setup}] Discord free: {env[0]}, then "
                            f"{name}: web is not updated, said, still "
                            f"pending",
                            "update_container@local:web" not in _ran(last)
                            and _said(last, said) and "web@local" in left
                            and not esc,
                            f"out {last.get('out')!r}, events "
                            f"{last.get('events')!r}, pending {left!r}, "
                            f"escapes {esc!r}"))
    return out


def web_free_checks():
    out = []
    P = t5.P
    rec, esc = t5.run_scenario("multi", [
        ("pin", "local", "web"), P("/api/update", ("name", "web"))])
    st = rec["steps"]
    out.append(("[multi] Web free: pinned, then its Update button: not "
                "updated, the page says not updated, still pending",
                st[-1].get("http") == "303 | Location: /?update=pinned&n=1"
                and _ran(st[-1]) == [] and "web@local" in _pending(rec)
                and not esc,
                f"http {st[-1].get('http')!r}, events "
                f"{st[-1].get('events')!r}, pending {_pending(rec)!r}"))
    rec, esc = t5.run_scenario("multi", [
        ("pin", "nas", "web"),
        P("/api/bulk", ("action", "update"), ("names", "web,nas/web"))])
    st = rec["steps"]
    out.append(("[multi] Web free: bulk web,nas/web with nas's pinned: the "
                "local web runs, nas's does not and is said, the page "
                "counts it",
                st[-1].get("http")
                == "303 | Location: /?update=started&held=1"
                and _ran(st[-1]) == ["update_container@local:web"]
                and _said(st[-1], HELD_PIN.format(NWEB))
                and _pending(rec) == ["web@nas"] and not esc,
                f"http {st[-1].get('http')!r}, out {st[-1].get('out')!r}, "
                f"events {st[-1].get('events')!r}, "
                f"pending {_pending(rec)!r}"))
    # A label is read by the worker, right before the update; the page
    # answered before that and could not know.
    rec, esc = t5.run_scenario("multi", [
        ("label", "local", "web", "docksentry.pin", "true"),
        P("/api/update", ("name", "web"))])
    st = rec["steps"]
    out.append(("[multi] Web free: a docksentry.pin label, then its Update "
                "button: not updated, the worker names the label",
                _ran(st[-1]) == [] and _said(st[-1],
                                             HELD_PIN_LABEL.format(LWEB))
                and "web@local" in _pending(rec) and not esc,
                f"http {st[-1].get('http')!r}, out {st[-1].get('out')!r}, "
                f"events {st[-1].get('events')!r}"))
    rec, esc = t5.run_scenario("multi", [
        ("major", "local", "web"), ("pin", "local", "web"),
        P("/api/major_confirm", ("name", "web"), ("action", "confirm"))])
    st = rec["steps"]
    major = rec["state"]["files"].get("major_confirmations.json", "")
    # Since Q16h the page says it (the pinned banner) and no thread is
    # started, so Telegram says nothing for a click made on the page.
    out.append(("[multi] Web free: a deferred major, then pinned, then "
                "confirmed: not updated, the page says so, the major stays",
                st[-1].get("http") == "303 | Location: /?update=pinned&n=1"
                and _ran(st[-1]) == []
                and not any(HELD_PIN.format("`web`") in o
                            for o in (st[-1].get("out") or ()))
                and '"web"' in major and not esc,
                f"http {st[-1].get('http')!r}, out {st[-1].get('out')!r}, "
                f"events {st[-1].get('events')!r}, majors {major!r}"))
    # Busy, the one asked for running and another pinned: both are said
    # (Q16f said only "held").
    row = t5.Row("multi")
    try:
        to = row.handler._queue_redirect(
            None, [("web", "update_same_running", {}),
                   ("nas/web", "update_not_queued_pinned", {})])
    finally:
        shutil.rmtree(row.data, ignore_errors=True)
    out.append(("Web: busy, one running and one pinned: the page says both",
                to == "/?update=same_running&held=1", f"to {to!r}"))
    # The pin list broken between the handler's question and the claim
    # (the race): the page's pin-list answer, the claim given back, no
    # worker started.
    import contextlib
    import io
    row = t5.Row("multi")
    try:
        with open(row.cfg.pinned_file, "wb") as f:
            f.write(b'["db"')
        h = row.handler.__new__(row.handler)
        started = []
        with contextlib.redirect_stdout(io.StringIO()):
            to = h._start_web_update(["web"], lambda *a: started.append(a),
                                     "web")
        locked = row.engine._update_lock.locked()
    finally:
        shutil.rmtree(row.data, ignore_errors=True)
    out.append(("Web free: the pin list unreadable at the claim: the "
                "pin-list answer, the lock given back, nothing started",
                to == "/?update=pinlist" and not locked and started == [],
                f"to {to!r}, locked {locked!r}, started {started!r}"))
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
                     telegram_free_checks, discord_free_checks,
                     web_free_checks, engine_checks):
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
