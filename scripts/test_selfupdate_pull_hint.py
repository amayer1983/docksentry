#!/usr/bin/env python3
"""A denied pull says which of the two things it probably is.

The pull lives in the neutral `selfupdate` module since the core was
pulled out of the bots, so that is the file this reads.

`pull access denied ... repository does not exist` is the daemon's single
answer to two unrelated situations: an image built on this machine, which
has nothing to pull from at all, and a private registry that simply wants
credentials. Docker's own words fit neither, and the first thing anyone
who builds Docksentry themselves sees on `/selfupdate` is that sentence.

Naming one cause would be a confident guess. Naming both is the help —
the reader knows which of the two they are, and we do not.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import i18n  # noqa: E402

checks = {}

src = open(os.path.join(os.path.dirname(__file__), "..",
                        "app", "selfupdate.py")).read()
i = src.index('ctx.t("selfupdate_failed_pull"')
block = src[i - 400:i + 700]

checks["the hint is only added when the pull was denied"] = (
    "access denied" in block and "repository does not exist" in block)
checks["…matched without caring about case"] = (".lower()" in block)
checks["…and it rides along with the raw error, not instead of it"] = (
    "selfupdate_failed_pull" in block and "selfupdate_pull_denied_hint" in block)
checks["a pull that failed for another reason is unchanged"] = (
    block.index("selfupdate_failed_pull") < block.index("access denied"))

en = i18n.get_translator("en")
hint = en("selfupdate_pull_denied_hint", image="ds/ds:latest")
checks["the image is named, not described"] = ("ds/ds:latest" in hint)
checks["both causes are offered"] = (
    "locally" in hint.lower() and "private" in hint.lower())
checks["…and the credentials are named so they can be set"] = (
    "DOCKER_USERNAME" in hint)
checks["neither cause is asserted over the other"] = (
    " or " in hint.lower())

for lang in sorted(os.listdir(os.path.join(os.path.dirname(__file__), "..",
                                           "app", "lang"))):
    code = lang[:-5]
    t = i18n.get_translator(code)
    checks[f"{code} has the hint, with the image in it"] = (
        "ds/ds:latest" in t("selfupdate_pull_denied_hint", image="ds/ds:latest"))

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
