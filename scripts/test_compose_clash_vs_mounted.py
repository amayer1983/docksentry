#!/usr/bin/env python3
"""Refusing a mount and having one already are not the same answer.

@NotRetarded mounted his stack directory at `/app/data/stacks` on
01.09. because I told him to. The container page then told him, for
three weeks: *"There is no mount that can fix this here: Docksentry
keeps its own data at /app/data/stacks, and mounting anything else
there would hide it. Move Docksentry's data elsewhere (DATA_DIR)."*

None of that was true for him. The guard compared the suggested mount
point against **every** destination Docksentry has mounted — so the
mount he had just added on my advice looked like our own data
directory, and the page sent him off to move `DATA_DIR` instead of
telling him his source was wrong. He went round that loop until the
screenshot arrived.

The guard exists for a real case and keeps it: a suggestion landing on
our own data directory would hide our state, and `/data` is that case,
because it is ours and Portainer's. What it must not do is mistake the
user's own fix for it.

Three outcomes now, and they are different sentences: refuse (it would
shadow our data), correct (you mount it already, the source is wrong),
advise (nothing is mounted, here is the line).
"""
import os
import re
import sys

checks = {}
ROOT = os.path.join(os.path.dirname(__file__), "..")
web = open(os.path.join(ROOT, "app", "web_ui.py"), encoding="utf-8").read()
blk = web.split("def _compose_mount_block")[1].split("\n        def ")[0]

# ── the refusal is about our data directory, nothing else ────────────
checks["the refusal is measured against our data directory"] = (
    'getattr(config, "data_dir"' in blk)
checks["…and no longer against every mount we hold"] = (
    "& self._own_mount_dests()" not in blk)
checks["…including a mount that merely contains it"] = (
    "_data.startswith(" in blk)

# ── an existing mount gets its own answer ────────────────────────────
checks["a mount we already have is a different finding"] = (
    "web_compose_mount_wrong_source" in blk)
checks["…and it names the source to go and check"] = (
    "_own_mount_sources(" in blk)
# The daemon already told us which container holds the file, so the
# right source is known. Saying only "yours is wrong" would throw that
# away — which is what the clash branch did for three weeks.
checks["…and the one that would work, when the daemon knows it"] = (
    "web_compose_mount_use_instead" in blk)
checks["…only when it differs from what they have"] = (
    "_right != _have" in blk)
checks["the refusal still comes first"] = (
    blk.index("web_compose_mount_clash") < blk.index("web_compose_mount_wrong_source"))

# ── the wording says which of the three it is ────────────────────────
import json                                                # noqa: E402
en = json.load(open(os.path.join(ROOT, "app", "lang", "en.json"), encoding="utf-8"))
clash, wrong = en["web_compose_mount_clash"], en["web_compose_mount_wrong_source"]
checks["the refusal explains what it would hide"] = "DATA_DIR" in clash
checks["the correction does not send anyone to DATA_DIR"] = "DATA_DIR" not in wrong
checks["…it points at the source instead"] = (
    "{src}" in wrong and "{dest}" in wrong and "wrong directory" in wrong)
checks["every language carries it"] = all(
    "web_compose_mount_wrong_source" in json.load(
        open(os.path.join(ROOT, "app", "lang", f), encoding="utf-8"))
    for f in os.listdir(os.path.join(ROOT, "app", "lang")) if f.endswith(".json"))

# ── the same question, answered the same way off the page ────────────
# `/audit` and the update notification ask it through `covering_mount`.
# A page that disagreed with them about one container would be worse
# than a page that stayed quiet.
sc = open(os.path.join(ROOT, "app", "selfcheck.py"), encoding="utf-8").read()
checks["the chats answer it through the shared helper"] = (
    "compose_paths.covering_mount(" in sc)
uc = open(os.path.join(ROOT, "app", "update_checker.py"), encoding="utf-8").read()
checks["…and so does the update note"] = (
    "compose_paths.covering_mount(" in uc)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
