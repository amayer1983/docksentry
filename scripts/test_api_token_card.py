#!/usr/bin/env python3
"""The one setting the interface said nothing at all about.

`API_TOKENS` grants read-only access to `/metrics` and `GET /api/status`
without the Web UI password. It is environment-only, which is defensible —
they are secrets, and the page that would edit them is reachable with a
*different* secret. What was not defensible: the interface didn't say
whether any existed. "Is my scraper authorised, or is it getting 401s?"
could only be answered by reading the compose file and then the container's
logs.

So the Connections page lists the token *names* and when each was last seen.
Never the tokens themselves — that is the whole point of the card, and this
file exists mostly to keep it that way.

Two things found while building it, both now asserted here:

**A saved empty list beats a set environment variable.** `api_tokens` is a
persistent key, so `"api_tokens": []` in `settings.json` overrules
`API_TOKENS=prom:…` — the variable sits in the compose file, plainly set,
doing nothing, and every request is refused. Measured: with the saved key
present, a correct token got 401; with it removed, 200.

**`API_TOKENS=prom` — no colon, no secret — can never match anything.** It
looks configured and protects nothing.
"""

import json
import os
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

WEB_UI = os.path.join(os.path.dirname(__file__), "..", "app", "web_ui.py")
LANG = os.path.join(os.path.dirname(__file__), "..", "app", "lang")

checks = {}
src = open(WEB_UI, encoding="utf-8").read()


def card_source():
    i = src.index("def _api_token_card")
    return src[i:src.index("\n        def _help", i)]


card = card_source()

# ── the tokens themselves never reach the page ───────────────────────
# The card renders `entry.partition(":")` and must use the part BEFORE the
# colon and nothing else. Asserted against the source rather than a
# rendered page: a page test proves the current inputs are safe, this
# proves the code cannot leak a token it was never given.
checks["the card splits each entry on the colon"] = 'partition(":")' in card
checks["…and renders only the name"] = (
    "_e(label)" in card and "_e(token" not in card)
# The secret half never reaches the output: no `{...token...}` anywhere in
# an interpolation. Checked this way rather than by counting mentions,
# because the only thing that matters is whether it can be *rendered*.
checks["…the secret half is never interpolated into the page"] = not [
    m for m in re.findall(r"\{[^{}]*\}", card)
    if re.search(r"\btoken\b", m) and "token_seen" not in m
    and "api_tokens" not in m]
checks["…and is only ever tested for emptiness"] = (
    "if not token.strip():" in card)
# A form would imply it can be saved here. It cannot — the value lives in
# the environment, and a field that silently fails to save is worse than
# no field at all.
checks["it is not a form"] = (
    "<input" not in card and "<textarea" not in card)

# ── the states it has to distinguish ─────────────────────────────────
checks["it says when nothing is configured"] = "web_api_tokens_none" in card
checks["it flags an entry with no secret after the colon"] = (
    "web_api_tokens_broken" in card and "token.strip()" in card)
checks["it says when the environment is being overruled"] = (
    'env_override("api_tokens")' in card and
    "web_api_tokens_overruled" in card)
# The overruling notice must appear in BOTH renderings — the empty case is
# exactly where it matters most, because that is the state a set variable
# gets overruled INTO.
checks["…in the empty case too, which is where it matters"] = (
    card.count("{overruled}") == 2)
checks["…and the notice never leaks the values either"] = (
    "web_api_tokens_overruled" in card and "config.api_tokens" not in
    card.split("overruled =")[1].split("if not configured")[0])

# ── last-used is recorded at one seam ────────────────────────────────
# `_api_token_name` is what both endpoints authenticate through, so the
# third endpoint to be added is covered without anyone remembering to.
i = src.index("def _api_token_name")
seam = src[i:src.index("\n        def ", i + 10)]
checks["usage is recorded where the token is verified"] = (
    "token_seen" in seam and "time.time()" in seam)
checks["…only after compare_digest succeeds"] = (
    seam.index("compare_digest") < seam.index("token_seen"))
checks["…and nowhere else"] = src.count("seen[label] = time.time()") == 1
# In memory on purpose: a Prometheus scraper hits /metrics every few
# seconds and a file written that often would be a lot of I/O to learn
# "still being scraped". The page has to admit that a restart clears it,
# or "never used" reads as "safe to revoke".
checks["the page admits a restart clears the record"] = (
    "web_api_tokens_hint" in card)
en = json.load(open(os.path.join(LANG, "en.json"), encoding="utf-8"))
checks["…in those words"] = "restart" in en["web_api_tokens_hint"].lower()
checks["the empty-usage wording does not claim 'never'"] = (
    "never" not in en["web_api_tokens_never"].lower())

# ── the strings exist, and fall back rather than showing keys ────────
KEYS = ["web_api_tokens", "web_api_tokens_intro", "web_api_tokens_none",
        "web_api_tokens_name", "web_api_tokens_last", "web_api_tokens_never",
        "web_api_tokens_hint", "web_api_tokens_broken",
        "web_api_tokens_overruled"]
checks["every string used by the card exists in English"] = all(
    k in en for k in KEYS)
de = json.load(open(os.path.join(LANG, "de.json"), encoding="utf-8"))
checks["…and in German"] = all(k in de for k in KEYS)
# The other 14 languages fall back to English (i18n: strings.get(key,
# en_strings.get(key, key))), so a missing key shows English rather than a
# raw `web_api_tokens_none` — which is why they are not required here.
i18n = open(os.path.join(os.path.dirname(__file__), "..", "app", "i18n.py"),
            encoding="utf-8").read()
checks["a missing translation falls back to English, not to the key"] = (
    "en_strings.get(key, key)" in i18n)
checks["no string mentions a token value"] = not any(
    "demo-token" in en[k] or "prom:" in en[k] for k in KEYS)

# ── the audit trail can be found by its own name ─────────────────────
# The heading is translated in all 16 languages and none of them contains
# "audit", so searching the page for the word everyone actually uses — the
# word in the docs, the changelog and the issue tracker — found nothing on
# the one page that has it.
i = src.index('id="audit"')
head = src[i:i + 300]
checks["the audit section has an anchor"] = 'class="card" id="audit"' in src
checks["…and carries the untranslated word"] = "Audit-Trail" in head
checks["…as a quieter second label, not as the heading"] = (
    'class="h2-alt"' in head)
css = open(os.path.join(os.path.dirname(__file__), "..", "app", "static",
                        "app.css"), encoding="utf-8").read()
checks["…and that class is styled"] = ".h2-alt {" in css
# It must not be a translated string, or it would be translated away again
# in the 15 languages where it is needed most.
checks["…and is not itself translatable"] = 't("Audit' not in head

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
