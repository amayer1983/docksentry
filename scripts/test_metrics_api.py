#!/usr/bin/env python3
"""The machine-readable endpoints and the tokens that guard them.

`/metrics` was the highest-reacted issue in two of the five projects
surveyed — around 120 reactions on the one idea in diun alone, with four
community PRs over four years. The motive that generalises best: people who
will not allow unattended updates run the tool in report-only mode, and for
them the metric IS the product.

Tokens come with it rather than after it. A Prometheus scraper cannot log
in, and handing it the shared Web UI password would give a monitoring job
the ability to stop containers.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from web_ui import _metric_label


def main():
    checks = {}

    # ── label escaping ───────────────────────────────────────────
    # Container names are user-controlled text in a format where an
    # unescaped quote breaks the LINE — and a malformed line costs the
    # scraper the whole response, not just that metric.
    checks["plain text is untouched"] = _metric_label("nginx") == "nginx"
    checks["a quote is escaped"] = _metric_label('a"b') == 'a\\"b'
    checks["a backslash is escaped"] = _metric_label("a\\b") == "a\\\\b"
    # A newline cannot be escaped into a label value at all — the format is
    # line-based — so it is removed rather than passed through.
    checks["a newline is removed"] = "\n" not in _metric_label("a\nb")
    checks["empty is safe"] = _metric_label("") == ""
    checks["None is safe"] = _metric_label(None) == ""
    # Escaping a backslash must not then escape the escape.
    checks["backslash then quote"] = _metric_label('a\\"b') == 'a\\\\\\"b'

    # ── the token comparison ─────────────────────────────────────
    import inspect as _i
    import web_ui
    src = _i.getsource(web_ui.create_handler)
    # Secrets: `==` leaks length and prefix through timing.
    checks["tokens are compared in constant time"] = "compare_digest" in src
    # A token that was presented and rejected must not fall through to the
    # password check — on an instance with no WEB_PASSWORD that answers
    # 200, so a revoked token would appear to keep working and the operator
    # would believe they had cut access when they had not.
    checks["a wrong token is rejected, not passed on"] = (
        "_api_token_supplied" in src and "Invalid API token" in src)
    # Read-only: the token is checked only for these two paths, so it can
    # never reach an endpoint that changes anything.
    checks["only the two read paths accept a token"] = (
        'in ("/metrics", "/api/status")' in src)
    checks["the token check is GET-only"] = "_api_token_name" not in _i.getsource(
        web_ui.create_handler).split("def do_POST")[-1]

    # ── metric naming ────────────────────────────────────────────
    # `_total` is reserved for counters; these are gauges. promtool
    # rejects the mismatch, which is how the original name was caught.
    checks["no gauge carries a _total suffix"] = (
        "docksentry_containers_total" not in src)
    checks["the gauge is named plainly"] = "docksentry_containers " in src

    # ── reading, never triggering ────────────────────────────────
    # An endpoint that ran a check would let anyone with a scrape interval
    # drive the update loop.
    state_src = src.split("def _machine_state")[1].split("def ")[0]
    checks["gathering does not start a check"] = "check_all" not in state_src

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
