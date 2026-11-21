#!/usr/bin/env python3
"""Every core Reply supplies exactly the placeholders its template wants.

`/note web GPU box` answered "📝 Note on `web`: —". The extraction into
`container_flags` renamed the parameter from `text` to `note` while the
template in all sixteen languages still said `{text}`, and `i18n`
renders a missing placeholder as an em dash rather than raising. So the
note was saved correctly and the confirmation showed nothing — in both
chats at once, because both now go through the same core.

Nothing caught it: the language check compares KEYS between files, and
the container-flags test checks that the keys EXIST. Neither compares a
call site's parameters against the sentence it fills in.

Reading the keys needs care. They are not all literals — `set_note`
picks between `note_set` and `note_cleared` with a ternary, which is
exactly the shape my first version of this scan skipped, certifying
zero while the bug it was written for sat in the file.
"""
import ast
import json
import os
import string
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
EN = json.load(open(os.path.join(APP, "lang", "en.json"), encoding="utf-8"))
CORE = ("container_flags.py", "lifecycle.py")

checks = {}


def placeholders(text):
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


def keys_of(node):
    """Every key a Reply's first argument can evaluate to."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):                    # a if c else b
        return keys_of(node.body) + keys_of(node.orelse)
    if isinstance(node, ast.BoolOp):                   # a or b
        out = []
        for v in node.values:
            out += keys_of(v)
        return out
    return []                                          # dynamic — skip


def params_of(node):
    """The parameter names a Reply call passes, or None if not a literal."""
    dict_node = None
    if len(node.args) > 1 and isinstance(node.args[1], ast.Dict):
        dict_node = node.args[1]
    for kw in node.keywords:
        if kw.arg == "params" and isinstance(kw.value, ast.Dict):
            dict_node = kw.value
    if dict_node is None:
        return None
    if any(k is None for k in dict_node.keys):         # **spread
        return None
    if not all(isinstance(k, ast.Constant) for k in dict_node.keys):
        return None
    return {k.value for k in dict_node.keys}


missing, unknown, spare, seen = [], [], [], 0
for mod in CORE:
    tree = ast.parse(open(os.path.join(APP, mod), encoding="utf-8").read())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "Reply"):
            continue
        if not node.args:
            continue
        for key in keys_of(node.args[0]):
            if not key:                                # Reply("") — the
                continue                               # `text` carrier
            seen += 1
            if key not in EN:
                unknown.append(f"{mod}:{node.lineno} {key}")
                continue
            given = params_of(node)
            if given is None:                          # forwarded params
                continue
            want = placeholders(EN[key])
            for p in sorted(want - given):
                missing.append(f"{mod}:{node.lineno} {key} → {{{p}}}")
            # A spare parameter on a call that picks between two keys is
            # expected — one dict serves both, and the shorter sentence
            # naturally uses less of it. A spare on a call with ONE key
            # is a value someone measured and nobody ever sees.
            if len(keys_of(node.args[0])) == 1:
                for p in sorted(given - want):
                    spare.append(f"{mod}:{node.lineno} {key} ← {p}")

checks["the scan actually found Reply calls"] = seen >= 30
# The ternary case specifically — the shape that hid the bug.
checks["…including keys chosen by a ternary"] = any(
    "note_set" in x or "note_cleared" in x
    for x in [f"{k}" for mod in CORE
              for node in ast.walk(ast.parse(
                  open(os.path.join(APP, mod), encoding="utf-8").read()))
              if isinstance(node, ast.Call)
              and getattr(node.func, "id", "") == "Reply" and node.args
              for k in keys_of(node.args[0])])

checks["no core Reply names a key that does not exist"] = unknown == []
if unknown:
    print("  unbekannt:", "; ".join(unknown))

checks["every placeholder a template wants is supplied"] = missing == []
if missing:
    print("  fehlend:", "; ".join(missing))

# A parameter nobody shows is not a crash — str.format ignores it — but
# it means someone measured a value the reader never sees. Worth knowing,
# not worth failing on its own; these are listed and pinned by count so a
# new one has to be a decision.
# Two remain, both a usage hint that deliberately says what IS accepted
# rather than repeating what you typed. Pinned by count so a third has
# to be a decision rather than an accident.
KNOWN_SPARE = 2
checks[f"no value is measured and then dropped (known: {KNOWN_SPARE})"] = (
    len(spare) <= KNOWN_SPARE)
if spare:
    print("  ungenutzt:", "; ".join(spare))

# ── the engine's busy answers (Q7) ──────────────────────────────────
# `claim_or_queue` and `claim_or_queue_many` answer with a key and its
# parameters; every connection fills in the container's name itself and
# renders a `<x>_seconds` value as `{x}` ("2m"). So for each answer the
# engine can give, in every language: the key exists, every placeholder
# is either one of those parameters or `name`, and no parameter is left
# with nowhere to go. Driven, not read off the source: the keys are the
# ones the engine actually returned, in every state it has.
import glob  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

sys.path.insert(0, APP)
from update_engine import UpdateEngine  # noqa: E402

LANGS = {os.path.basename(f)[:-5]: json.load(open(f, encoding="utf-8"))
         for f in sorted(glob.glob(os.path.join(APP, "lang", "*.json")))}


def _eng(queued=(), updating=(), held=True):
    e = UpdateEngine.__new__(UpdateEngine)
    e._update_queue, e._queued_at = [], {}
    e._update_queue_lock = threading.Lock()
    e._update_lock = threading.Lock()
    e._updating = {k: "" for k in updating}
    e._queued_selfupdate = None
    for k in queued:
        e.enqueue_update(k)
        e._queued_at[k] = time.time() - 90
    if held:
        e._update_lock.acquire()
    return e


answers = []          # (key, params), in the order the engine gave them
_full = [f"c{i}" for i in range(UpdateEngine.UPDATE_QUEUE_MAX)]
for coq in ("claim_or_queue", "claim_or_queue_many"):
    for state, kw, keys in (
            ("running", {"updating": ["web"]}, ["web"]),
            ("full", {"queued": _full}, ["web"]),
            ("already queued", {"queued": ["web"]}, ["web"]),
            ("queued", {}, ["web"])):
        e = _eng(**kw)
        f = getattr(e, coq, None)
        if f is None:
            checks[f"{coq} exists"] = False
            break
        if coq == "claim_or_queue":
            _c, key, params = f(keys[0])
            got = [(key, params)]
        else:
            _c, outcomes = f(keys)
            got = [(key, params) for _k, key, params in outcomes]
        checks[f"{coq}, {state}: answers busy"] = _c is False and len(got) == 1
        answers += got

_given = {key for key, _p in answers}
checks["the engine's busy answers are the four I know"] = _given == {
    "update_same_running", "update_queue_full", "update_already_queued",
    "update_queued"}


def _rendered(params):
    return {p[:-len("_seconds")] if p.endswith("_seconds") else p
            for p in params}


_bad = []
for key, params in answers:
    want = _rendered(params)
    for lang, strings in LANGS.items():
        if key not in strings:
            _bad.append(f"{lang}: no {key}")
            continue
        have = placeholders(strings[key])
        if have - want - {"name"}:
            _bad.append(f"{lang}: {key} wants {sorted(have - want - {'name'})}")
        if want - have:
            _bad.append(f"{lang}: {key} drops {sorted(want - have)}")
checks[f"…exist in all {len(LANGS)} languages with matching placeholders"] = (
    bool(answers) and not _bad and len(LANGS) >= 16)
if _bad:
    print("  busy answers:", "; ".join(_bad))

# ── Discord's lines for the same answers (Q15) ──────────────────────
# A busy /update or confirmed /updateall renders the engine's answers
# itself, in its own keys. Driven through the real `_queued_lines` on
# every answer the engine gave above: each sentence it asks for exists
# in every language with exactly the placeholders it fills.
from discord_bot import DiscordBot  # noqa: E402


class _Said:
    # The real one: it picks the line on where a queued result shows up
    # from the bot channel setting, and is driven both ways below.
    _result_where = getattr(DiscordBot, "_result_where", None)

    def __init__(self):
        self.asked = []
        self.config = type("Cfg", (), {})()

    def t(self, key, **kw):
        self.asked.append((key, set(kw)))
        return key


_said = _Said()
_lines = getattr(DiscordBot, "_queued_lines", None)
checks["Discord renders the engine's busy answers (_queued_lines)"] = (
    _lines is not None)
for _chan in ("", "1234"):
    _said.config.discord_bot_channel = _chan
    for key, params in answers if _lines is not None else ():
        _lines(_said, [("web", key, params)], {"web": "web"})
_dbad = []
for key, given in _said.asked:
    for lang, strings in LANGS.items():
        if key not in strings:
            _dbad.append(f"{lang}: no {key}")
        elif placeholders(strings[key]) != given:
            _dbad.append(f"{lang}: {key} wants "
                         f"{sorted(placeholders(strings[key]))}, gets "
                         f"{sorted(given)}")
checks["Discord says every busy answer, in all languages, placeholders "
       "filled"] = (
    {k for k, _g in _said.asked} == {
        "chan_update_queued", "chan_update_already_queued",
        "chan_update_queue_full", "update_same_running",
        "chan_update_result_channel", "chan_update_result_no_channel"}
    and not _dbad)
if _dbad:
    print("  Discord busy lines:", "; ".join(_dbad))

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
