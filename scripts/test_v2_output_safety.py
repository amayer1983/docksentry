#!/usr/bin/env python3
"""What the V2 page puts on screen cannot be turned into script.

Two holes, both found by the audit of 18.10.2026, both in the V2 path
and neither in V1 — which is the pattern worth remembering: the newer
surface did not inherit the controls built for the older one.

**The script block.** `shell()` wrote `config.language` into
`<script>window.DS_V2 = {...}</script>` through `json.dumps`. JSON
escaping is not HTML escaping: the HTML parser ends the block at the
literal text `</script`, wherever it sits, including inside a JSON
string. `config.language` is writable through the settings form and
through a backup bundle, and the bundle's allowlist checks the *key*
and never the *value*.

**The link.** `container_store.is_safe_link` describes itself in
`web_ui.py` as "THE choke point for defence in depth" and runs at five
places on the V1 path. On the V2 path it ran nowhere: the stored link
went to the client raw, and `v2.js` renders it into an `href` behind
`esc()`. HTML-escaping does not touch a URL scheme, so
`javascript:alert(1)` arrived intact and clickable.

Each layer is driven on its own below. The page has two of them —
`safe_language` rejects unknown codes, the escaper handles whatever
still gets through — and a check that only renders a page measures the
first one and says nothing about the second. That is the mistake this
file is written to avoid: the fix is the escaper, and `safe_language`
would hide its absence.
"""

import ast
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_v2  # noqa: E402
from i18n import available_languages, safe_language  # noqa: E402

checks = {}

# Payloads split so this file cannot close its own block when read as
# source, and so a grep for `</script>` in the tree does not hit here.
CLOSE = "</scr" + "ipt>"
PAYLOAD = CLOSE + "<scr" + "ipt>alert(1)//"


def _t(key, **kwargs):
    return key


# ── layer one: the escaper, driven directly ──────────────────────────
# Not through `shell()`. Through the function that does the work, with
# the value `shell()` would have rejected, because this is the layer
# that has to hold when something else lets a string through.

out = web_v2.script_json({"lang": PAYLOAD})
checks["the escaper does not emit a literal closing tag"] = CLOSE not in out
checks["…nor a bare '<' at all"] = "<" not in out
checks["…nor a bare '>'"] = ">" not in out
# `&` matters where the same text is read in an HTML-escaping context.
checks["…nor a bare '&'"] = "&" not in web_v2.script_json({"x": "a&b"})
# The two characters that end a line in JavaScript but not in JSON.
checks["…nor a raw U+2028 / U+2029"] = (
    " " not in web_v2.script_json({"x": "a b"})
    and " " not in web_v2.script_json({"x": "a b"}))

# The counter-direction, and the one that matters most: escaping that
# breaks the page is not a fix. The browser must read back exactly what
# went in, payload and all.
checks["…and the browser still parses it back"] = (
    json.loads(out) == {"lang": PAYLOAD})
checks["…unchanged, including non-ASCII"] = (
    json.loads(web_v2.script_json({"x": "Grüße ☕"})) == {"x": "Grüße ☕"})

# ── layer two: the language allowlist, driven directly ───────────────

_known = available_languages()
checks["the allowlist finds the languages we ship"] = (
    "en" in _known and len(_known) >= 2)
checks["a hostile language code is refused"] = (
    safe_language(PAYLOAD) == "en")
checks["…and so is an unknown but harmless one"] = (
    safe_language("zz") == "en")
checks["…and a non-string"] = (
    safe_language(None) == "en" and safe_language(["de"]) == "en"
    and safe_language(42) == "en")
# Counter-direction: a real language must survive, or "refuses
# everything" would pass every check above.
checks["a language we ship is kept"] = all(
    safe_language(code) == code for code in _known)
checks["…including when it arrives with a region suffix"] = (
    safe_language("de-DE") == "de" if "de" in _known else True)

# ── the two together, as the page actually renders ───────────────────

page = web_v2.shell(_t, "9.9.9", PAYLOAD)
checks["the rendered page has no injected script block"] = (
    CLOSE + "<scr" + "ipt>" not in page)
_data = re.search(r"window\.DS_V2 = (.*?);</script>", page)
checks["…and its data block is still valid JSON"] = (
    _data is not None and json.loads(_data.group(1))["lang"] == "en")
checks["…and the attribute carries no quote to break out of"] = (
    re.search(r'data-lang="([^"]*)"', page).group(1) == "en")

# Counter-direction for the page: a legitimate language reaches both
# the attribute and the data block. Without this the three checks above
# are satisfied by a page that renders nothing.
_real = "de" if "de" in _known else "en"
_ok = web_v2.shell(_t, "9.9.9", _real)
checks["a real language reaches the page"] = (
    f'data-lang="{_real}"' in _ok
    and json.loads(
        re.search(r"window\.DS_V2 = (.*?);</script>", _ok).group(1)
    )["lang"] == _real)

# ── the link, at the server, not in the client ───────────────────────

for bad in ("javascript:alert(1)",
            "JavaScript:alert(1)",
            "\tjavascript:alert(1)",
            "data:text/html,<scr" + "ipt>alert(1)</scr" + "ipt>",
            "//evil.example/x",
            "vbscript:msgbox(1)"):
    # `repr`, not `strip()`: the tab-prefixed and plain `javascript:`
    # entries produce the same stripped text, so the second silently
    # replaced the first in this dict and one of the six was never
    # reported. Same defect the audit catalogued as "f-string keys
    # without interpolation in a loop" — caught here by counting.
    checks[f"link refused: {bad!r}"] = web_v2._link(bad) == ""

for good in ("https://example.com/changelog",
             "http://192.168.1.10:8080/releases"):
    checks[f"link kept: {good[:34]}"] = web_v2._link(good) == good

checks["an empty link stays empty"] = web_v2._link("") == ""
checks["…and so does a missing one"] = web_v2._link(None) == ""

# The two blocks above prove `_link` works. They say nothing about
# whether anything CALLS it — a helper that is correct and unused is
# the shape this whole hole had in the first place.
#
# Read as a tree, not as text. A `grep` for the call would also be
# satisfied by the same characters sitting in a comment, which is the
# failure the project catalogued after `/hosts check` sat commented out
# behind three green checks.
_tree = ast.parse(open(os.path.join(os.path.dirname(__file__), "..", "app",
                                    "web_v2.py")).read())
_view = next(n for n in ast.walk(_tree)
             if isinstance(n, ast.FunctionDef) and n.name == "container_rows")
_link_keys = [
    kv for node in ast.walk(_view) if isinstance(node, ast.Dict)
    for k, kv in zip(node.keys, node.values)
    if isinstance(k, ast.Constant) and k.value == "link"]
checks["container_rows emits exactly one 'link' value"] = len(_link_keys) == 1
checks["…and it is the output of the safety check"] = bool(_link_keys) and (
    isinstance(_link_keys[0], ast.Call)
    and getattr(_link_keys[0].func, "id", None) == "_link")

# ── the outer page frame, which was the third place ─────────────────
# Found only by rendering a real instance with the payload in its
# settings: `web_ui` writes `<html lang="{...}">` twice, and it was
# taking `config.language` raw. It was never exploitable — `_e()` is
# html-escaping and holds — but it put the whole payload in the
# attribute, and relying on one escape call at the end of a chain is
# exactly the shape the other two holes had. Both go through
# `safe_language` now.
#
# Read as a tree. A regex over the file would be satisfied by the same
# text in a comment.
_web_tree = ast.parse(open(os.path.join(os.path.dirname(__file__), "..",
                                        "app", "web_ui.py")).read())
_lang_frames = []
for node in ast.walk(_web_tree):
    if not isinstance(node, ast.JoinedStr):
        continue
    text = "".join(v.value for v in node.values
                   if isinstance(v, ast.Constant) and isinstance(v.value, str))
    if "<html lang=" not in text:
        continue
    _lang_frames.append(any(
        isinstance(v, ast.FormattedValue)
        and "safe_language" in ast.dump(v.value)
        for v in node.values))

checks["the page frame sets a lang attribute at all"] = len(_lang_frames) >= 2
checks["…and every one of them is filtered"] = (
    bool(_lang_frames) and all(_lang_frames))

# A count, because a dict silently swallows a duplicate key and every
# ✅ above would still print. 6 refused + 2 kept = 8 keys beginning
# "link "; the two empty-input cases are worded differently on purpose
# and are not counted here.
checks["every link case was actually measured"] = (
    len([k for k in checks if k.startswith("link ")]) == 8)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
