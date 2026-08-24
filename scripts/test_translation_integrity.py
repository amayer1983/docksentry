#!/usr/bin/env python3
"""A translation must carry everything the English one carries.

Fourteen of the sixteen languages were roughly half untranslated —
`pre-commit-check.py` compared KEY SETS between files and reported
"✅ All 16 languages in sync" while 397 sentences per language were still
verbatim English. The claim was true on the level it measured and false
on the level anyone cares about.

Filling those in is a big mechanical edit, and mechanical edits to
sentences break the parts that are not sentences. This checks the parts:

  * placeholders — a dropped `{name}` renders as an em dash and a
    stray one crashes the format call
  * backticks — Telegram and Discord both read them as code spans, and
    an odd number swallows the rest of the message
  * the leading emoji — it is the status marker, and a message that
    loses its ✅ reads as a different outcome
  * newlines — several keys are multi-line by design

None of this judges whether a translation is GOOD. It judges whether it
is still the same message. Quality is a question for someone who speaks
the language, and this file does not pretend otherwise.
"""
import glob
import json
import os
import re
import string
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
LANGS = sorted(glob.glob(os.path.join(APP, "lang", "*.json")))
EN = json.load(open(os.path.join(APP, "lang", "en.json"), encoding="utf-8"))

checks = {}
checks["all 16 languages are examined"] = len(LANGS) == 16

LEAD_EMOJI = re.compile(
    r"^([\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍\s]+)")


def placeholders(text):
    return sorted(f for _, f, _, _ in string.Formatter().parse(text) if f)


bad_ph, bad_tick, bad_emoji, bad_nl, unparsable = [], [], [], [], []

for path in LANGS:
    code = os.path.basename(path)[:-5]
    if code == "en":
        continue
    d = json.load(open(path, encoding="utf-8"))
    for key, src in EN.items():
        if not isinstance(src, str):
            continue
        dst = d.get(key)
        if not isinstance(dst, str):
            continue
        try:
            want, got = placeholders(src), placeholders(dst)
        except ValueError as e:
            unparsable.append(f"{code}/{key}: {e}")
            continue
        if want != got:
            bad_ph.append(f"{code}/{key}: {want} → {got}")
        if dst.count("`") % 2:
            bad_tick.append(f"{code}/{key}")
        # The status marker. Compared as a set, because languages that
        # read right-to-left legitimately reorder a leading pair.
        e_src = set((LEAD_EMOJI.match(src) or [""])[0].strip())
        e_dst = set((LEAD_EMOJI.match(dst) or [""])[0].strip())
        if e_src and e_src != e_dst:
            bad_emoji.append(f"{code}/{key}: {sorted(e_src)} → {sorted(e_dst)}")
        if src.count("\n") != dst.count("\n"):
            bad_nl.append(f"{code}/{key}: {src.count(chr(10))} → "
                          f"{dst.count(chr(10))}")

for label, found in (("every placeholder survives translation", bad_ph),
                     ("no message has an unbalanced backtick", bad_tick),
                     ("the leading status emoji survives", bad_emoji),
                     ("multi-line messages keep their lines", bad_nl),
                     ("every message can be formatted at all", unparsable)):
    checks[label] = found == []
    if found:
        for x in found[:12]:
            print("   ", x)
        if len(found) > 12:
            print(f"    … und {len(found) - 12} weitere")

# How much is still English. Not a failure — it is the work list, and
# printing it is how the number stops being invisible the way it was.
def translatable(v):
    core = re.sub(r"[{][^}]*[}]|https?://\S+|`[^`]*`|[^\w\s]", " ", v)
    w = [x for x in core.split() if len(x) > 2 and x.lower() not in
         ("docksentry", "telegram", "discord", "gotify", "matrix", "apprise",
          "ntfy", "docker", "podman", "smtp", "http", "https", "url", "json",
          "id", "cron")]
    return len(w) >= 2


# Two ways to still be English, and only the first is obvious. A key can
# be byte-identical to `en.json` — never touched. Or it can differ from
# `en.json` and still be English, because the English side was reworded
# afterwards and the translation was left behind. Byte-equality misses
# that entirely; a Dutch translator found five such keys by reading them,
# which is not a method that scales to fifteen languages.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lang_todo import NON_LATIN, latin_prose, looks_english  # noqa: E402

tr = [k for k, v in EN.items() if isinstance(v, str) and translatable(v)]
remaining, stale = {}, {}
for path in LANGS:
    code = os.path.basename(path)[:-5]
    if code == "en":
        continue
    d = json.load(open(path, encoding="utf-8"))
    n = sum(1 for k in tr if d.get(k) == EN[k])
    st = [k for k in tr
          if isinstance(d.get(k), str) and d[k] != EN[k]
          and looks_english(d[k])]
    if n:
        remaining[code] = n
    if st:
        stale[code] = st
if stale:
    print("  veraltetes Englisch (nicht byte-gleich, liest sich englisch):")
    for c, ks in sorted(stale.items(), key=lambda x: -len(x[1])):
        print(f"    {c}: {len(ks)}  z.B. {', '.join(sorted(ks)[:4])}")

# In a file written in another script, Latin prose IS the evidence — and
# a far stronger one than any word list. `help_detail_lifecycle` sat
# untranslated in Ukrainian carrying an OLD English wording: neither
# byte-identical to en.json nor rich enough in function words to trip the
# stopword test. A translator found it by reading, which does not scale.
latin = {}
for path in LANGS:
    code = os.path.basename(path)[:-5]
    if code not in NON_LATIN:
        continue
    d = json.load(open(path, encoding="utf-8"))
    ks = [k for k, v in d.items() if isinstance(v, str) and latin_prose(v)]
    if ks:
        latin[code] = ks
if latin:
    print("  lateinischer Fließtext in nicht-lateinischer Schrift:")
    for c, ks in sorted(latin.items(), key=lambda x: -len(x[1])):
        print(f"    {c}: {len(ks)}  z.B. {', '.join(sorted(ks)[:4])}")
if remaining:
    print("  noch englisch: " + ", ".join(
        f"{c} {n}" for c, n in sorted(remaining.items(),
                                      key=lambda x: -x[1])))
print(f"  (von {len(tr)} übersetzbaren Keys)")

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
