#!/usr/bin/env python3
"""Every language file carries the same keys, and the suite says so.

This rule has always existed — `scripts/pre-commit-check.py` enforces it,
and CI runs that gate. What did not exist was anything in the suite that
*fails* when the rule goes away. `test_precommit_gate.py` runs the gate
and checks it exits zero, which is exactly what it says it does and all
it can do: delete the rule from the gate and the gate still exits zero.

So the mutation sweep of 07.11.2026 could switch the language check off
in `pre-commit-check.py` **and** remove `status_queued` from `de.json`,
and 214 procedures stayed green. The rule was real, the guard around it
was not.

This is the rule itself, driven against the files. It owns the promise;
the gate keeps enforcing it at commit time. If the gate's copy is ever
deleted, this goes red — which is what the ratchet needs to anchor on.

Why the promise matters: a key present in `en` and missing elsewhere is
not a cosmetic gap. `t()` falls back to the key name, so a German user
gets `status_queued` printed raw in the middle of a sentence. That is
how this was found the first time, on 23.10.2026 — green locally across
211 procedures, red in CI, twice.
"""

import json
import os
import sys

LANG_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "lang")

checks = {}

files = sorted(f for f in os.listdir(LANG_DIR) if f.endswith(".json"))

# The counter-direction, first and deliberately: every check below is a
# statement about a set, and an empty set satisfies all of them. One
# missing file would otherwise make this procedure quieter, not louder.
checks["there are sixteen language files"] = len(files) == 16
checks["…and English is one of them"] = "en.json" in files

loaded = {}
for f in files:
    path = os.path.join(LANG_DIR, f)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        ok = isinstance(data, dict)
        if ok:
            loaded[f[:-5]] = data
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        ok = False
        print(f"      → {f}: {type(e).__name__}: {e}")
    checks[f"{f[:-5]}: is a readable JSON object"] = ok

ref = set(loaded.get("en", {}))
checks["English carries a non-trivial set of keys"] = len(ref) > 500

for code in sorted(loaded):
    keys = set(loaded[code])
    missing = ref - keys
    extra = keys - ref
    checks[f"{code}: has every key English has"] = not missing
    if missing:
        print(f"      → {code} is missing {len(missing)}: "
              f"{', '.join(sorted(missing)[:8])}")
    # Both directions. A file with extra keys is the same defect seen
    # from the other end — somebody added a string to one language and
    # nowhere else, and English is the reference.
    checks[f"{code}: and no key English does not"] = not extra
    if extra:
        print(f"      → {code} has {len(extra)} extra: "
              f"{', '.join(sorted(extra)[:8])}")
    # An empty value falls back to nothing at all, which renders as a
    # blank where a sentence should be — worse than the English word.
    blank = [k for k, v in loaded[code].items()
             if isinstance(v, str) and not v.strip()]
    checks[f"{code}: no key is translated to an empty string"] = not blank
    if blank:
        print(f"      → {code} has {len(blank)} blank: "
              f"{', '.join(sorted(blank)[:8])}")

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
