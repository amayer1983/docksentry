#!/usr/bin/env python3
"""A long report must arrive, not vanish (#56, @LeeNX).

Three of his Cloudflare tunnels updated, all three failed their healthcheck,
all three rolled back cleanly — and he was told none of it. The updates
happened, the rollbacks happened, and no notification arrived at all. He
worked out the cause himself: the message was too big for Telegram.

`send_message` handed Telegram whatever it was given. Over 4096 characters
Telegram rejects the whole thing with `ok: false`; the code then retried
once WITHOUT Markdown — which does nothing about length — and returned the
failed result to a caller that does not look at it. Silent loss, and the
worst possible one: the report you only need when something went wrong is
also the longest.

Splitting lives in `send_message` rather than at the call sites because
there was already one hand-rolled split, inline in `/status`, and the path
producing the LONGEST messages did not have it. Same reasoning as the audit
trail's single seam: instrument the seam, not the 20 callers.

The fenced-block handling is not decoration. An update report carries
rollback logs in ``` blocks. A chunk that ends mid-fence renders as literal
backticks in one message and swallows the next one as code — which would
turn a truncation bug into a corruption bug.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from telegram_bot import split_for_telegram as split, TELEGRAM_LIMIT


def main():
    checks = {}

    # ── short messages are untouched ─────────────────────────────
    # The overwhelming majority. Any change in shape here would show up in
    # every notification this project sends.
    for text in ("hello", "", "a\nb\nc", "x" * (TELEGRAM_LIMIT - 1)):
        checks[f"unchanged at {len(text)} chars"] = split(text) == [text]

    # ── his report ───────────────────────────────────────────────
    block = ("Updating: cft-{i}-latest (cloudflare/cloudflared:latest)\n"
             "```\n"
             + "\n".join(f"  Health check [{n}, {n}0s/600s]: "
                         "state=running, health=starting" for n in range(1, 8))
             + "\n```\n"
             "  Health check FAILED (unhealthy) — rolling back\n")
    report = "".join(block.replace("{i}", str(i)) * 4 for i in range(3))
    parts = split(report)
    checks["a long report is split"] = len(parts) > 1
    checks["every chunk fits"] = all(len(p) <= TELEGRAM_LIMIT for p in parts)
    checks["nothing is dropped"] = (
        "".join(p.replace("```", "") for p in parts).replace("\n", "")
        == report.replace("```", "").replace("\n", ""))

    # ── fences survive the break ─────────────────────────────────
    # Each chunk must be self-contained: an odd number of fences means one
    # message opens a code block the next one is expected to close.
    checks["no chunk leaves a fence open"] = all(
        p.count("```") % 2 == 0 for p in parts)
    # And a break that lands inside a block must reopen it, or the second
    # half of the log renders as prose.
    fenced = "intro\n```\n" + "\n".join(f"line {i:04d} " + "y" * 60
                                        for i in range(200)) + "\n```\nend"
    fparts = split(fenced)
    checks["a fence is reopened after the break"] = (
        len(fparts) > 1 and fparts[1].lstrip().startswith("```"))
    checks["fenced content is all still there"] = all(
        f"line {i:04d}" in "".join(fparts) for i in (0, 99, 199))

    # ── a single unsplittable line ───────────────────────────────
    # A container name plus a digest plus a URL can exceed the limit with
    # no newline to break on. Cutting it is worse than nothing being sent
    # at all — which is what happened before.
    monster = "x" * (TELEGRAM_LIMIT * 2 + 50)
    mparts = split(monster)
    checks["an oversized single line is cut, not dropped"] = (
        len(mparts) == 3 and all(len(p) <= TELEGRAM_LIMIT for p in mparts))
    checks["…and keeps every character"] = "".join(mparts) == monster

    # ── the seam is where it belongs ─────────────────────────────
    src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "telegram_bot.py"), encoding="utf-8").read()
    send = src[src.index("    def send_message("):]
    send = send[:send.index("\n    def ", 10)]
    checks["send_message does the splitting"] = "split_for_telegram(" in send
    # Buttons act on the whole report; on a middle chunk they would sit
    # above text they do not describe.
    checks["buttons ride on the last chunk"] = "i == len(parts) - 1" in send
    # The Markdown retry must survive — it exists for parse errors, which
    # are a different failure from length.
    checks["the markdown retry is still there"] = 'data.pop("parse_mode"' in send

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    if failed:
        print(f"    chunks: {[len(p) for p in parts]}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
