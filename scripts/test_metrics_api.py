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
    import ast as _ast
    import inspect as _i
    import os as _os
    import types as _ty
    import web_ui
    src = _i.getsource(web_ui.create_handler)

    # Secrets: a plain `==` leaks their length and prefix through timing.
    #
    # This was `"compare_digest" in src`, and the word also appears in
    # the docstring three lines above the call — so rewording the
    # comment would have broken the check while swapping the call for
    # `==` would not. Two things are asked instead: that the comparison
    # really is the constant-time one, read as a tree, and that the
    # whole token path behaves.
    _mod = _ast.parse(open(web_ui.__file__, encoding="utf-8").read())
    _calls = [n for n in _ast.walk(_mod)
              if isinstance(n, _ast.Call)
              and isinstance(n.func, _ast.Attribute)
              and n.func.attr == "compare_digest"]
    checks["tokens are compared in constant time"] = bool(_calls)
    # …and nothing in that function compares a token with `==`, which is
    # the mistake the constant-time call exists to avoid.
    _fn = next((n for n in _ast.walk(_mod)
                if isinstance(n, _ast.FunctionDef)
                and n.name == "_api_token_name"), None)
    _eq = [n for n in _ast.walk(_fn or _ast.Module(body=[], type_ignores=[]))
           if isinstance(n, _ast.Compare)
           and any(isinstance(o, (_ast.Eq, _ast.NotEq)) for o in n.ops)
           and "supplied" in (_ast.dump(n))]
    checks["…and never with a plain =="] = _eq == []

    # Driven: the right token is recognised, a wrong one is not, and
    # neither a token that shares a prefix nor one that shares a suffix
    # gets in. Those two are what a timing attack builds towards, and
    # they are also the cheapest way to catch a `startswith` creeping in.
    def _supplied(header=None, query=None, tokens=("prom:s3cret-token",)):
        handler_cls = web_ui.create_handler(
            _ty.SimpleNamespace(api_tokens=list(tokens), language="en",
                                web_password="", web_username=""),
            checker=None, bot=_ty.SimpleNamespace(t=None), store=None)
        h = handler_cls.__new__(handler_cls)
        h.path = f"/metrics?token={query}" if query else "/metrics"
        h.headers = {"Authorization": f"Bearer {header}"} if header else {}
        return h._api_token_name()

    checks["the right token in a Bearer header is recognised"] = (
        _supplied(header="s3cret-token") == "prom")
    checks["…and in a query string too, for scrapers that cannot set one"] = (
        _supplied(query="s3cret-token") == "prom")
    checks["a wrong token is not recognised"] = (
        _supplied(header="wrong-token") == "")
    checks["…nor one that shares the prefix"] = (
        _supplied(header="s3cret-tok") == ""
        and _supplied(header="s3cret-tokenX") == "")
    checks["…nor one that shares the suffix"] = (
        _supplied(header="Xs3cret-token") == ""
        and _supplied(header="token") == "")
    checks["…and no token at all is no token"] = _supplied() == ""
    # With nothing configured, nothing is a valid token — including the
    # empty string, which is what a bare `?token=` sends.
    checks["with no tokens configured, none is accepted"] = (
        _supplied(header="anything", tokens=()) == ""
        and _supplied(query="", tokens=()) == "")
    # Each token keeps its own name, so one can be revoked without
    # disturbing the other.
    checks["each token is reported under its own name"] = (
        _supplied(header="yyy", tokens=("prom:xxx", "grafana:yyy"))
        == "grafana")
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

    # ── a host that is down is reported as ITSELF ────────────────
    # The view for an unreachable host carried `unreachable` but no
    # `host`, and this fell back to LOCAL_HOST — so with one remote off,
    # /metrics and /api/status said the LOCAL machine was down and
    # dropped the one that actually was. An alert about the wrong box,
    # on exactly the multi-host installs where one being off is normal.
    # Shipped from 2026-08-01 until it turned up in a four-host test.
    views_src = src.split("def _host_views")[1].split("@staticmethod")[0]
    checks["every failed-host view names itself"] = (
        views_src.count('"unreachable": _host.name,')
        == views_src.count('"host": _host.name,')
        and views_src.count('"host": _host.name,') >= 1)
    checks["…and the reader never guesses local for it"] = (
        'v.get("host") or v.get("unreachable") or LOCAL_HOST' in state_src)

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
