#!/usr/bin/env python3
"""What `/metrics` and `/api/status` actually report.

`docs/web-ui.md:117-121` promises containers watched, updates pending,
a gauge per host and one per container that has an update, and the same
numbers again as JSON. On 10.10.2026 a sweep found **eight** reverts in
these two endpoints where not one of 194 procedures went red: delete
`docksentry_up`, pin a count to zero, rename a key, send the whole
thing as `text/html` — nothing noticed. No procedure called
`_serve_metrics` or `_serve_status_json` at all; the only thing near
them took the module's source apart as text.

That matters more here than in most places. People who will not let a
tool update their containers unattended run it in report-only mode, and
for them the metric IS the product — a gauge silently stuck at zero is
indistinguishable from "nothing needs doing", which is the one answer
it must never give wrongly.

So both endpoints are served for real and the bytes are read back. The
numbers come from a fixture with two hosts, one of them unreachable,
and containers on both — because a single-host fixture would pass on
code that reports only the first host it sees, and an all-reachable one
would pass on code that cannot say a host is down.
"""
import io
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_ui  # noqa: E402

checks = {}

#: Two hosts, one dead, four containers, two of them with updates. Every
#: number below is derivable from this by hand, which is the point.
VIEWS = [
    {"host": "local",
     "containers": [{"name": "web"}, {"name": "db"}],
     "pending": [{"name": "web"}]},
    {"host": "nas",
     "containers": [{"name": "jellyfin"}, {"name": "sonarr"}],
     "pending": [{"name": "sonarr"}]},
    {"host": "pve1", "unreachable": True},
]


class _Cfg:
    _CALLS = ("env_override", "save", "load", "apply", "write", "set_")

    language = "en"
    api_tokens = []
    web_password = ""
    web_username = ""

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name.startswith(self._CALLS):
            return lambda *a, **kw: None
        return ""


def _serve(which, views=VIEWS):
    """Call the real endpoint; return (status, headers, body)."""
    cls = web_ui.create_handler(_Cfg(), checker=None,
                                bot=types.SimpleNamespace(t=None), store=None)
    h = cls.__new__(cls)
    h.path = "/metrics" if which == "metrics" else "/api/status"
    h.headers = {}
    h.client_address = ("127.0.0.1", 0)
    h.rfile = types.SimpleNamespace(read=lambda n: b"")
    h.wfile = io.BytesIO()
    seen = {"status": None, "headers": {}}
    h.send_response = lambda code: seen.update(status=code)
    h.send_header = lambda k, v: seen["headers"].__setitem__(k.lower(), v)
    h.end_headers = lambda: None
    h._host_views = lambda: views
    getattr(h, "_serve_metrics" if which == "metrics"
            else "_serve_status_json")()
    return seen["status"], seen["headers"], h.wfile.getvalue().decode()


def _gauge(body, name, labels=""):
    """The value of one metric line, or None."""
    needle = f"{name}{labels} "
    for line in body.splitlines():
        if line.startswith(needle):
            return line[len(needle):].strip()
    return None


# ── /metrics ─────────────────────────────────────────────────────────
_status, _headers, _body = _serve("metrics")

checks["/metrics answers at all"] = _status == 200
# A scraper reads the content type. Served as text/html it is not a
# Prometheus endpoint, however right the bytes are.
checks["…as Prometheus text, not as a web page"] = (
    _headers.get("content-type", "").startswith("text/plain")
    and "version=0.0.4" in _headers.get("content-type", ""))

checks["it says the program is up"] = _gauge(_body, "docksentry_up") == "1"
checks["…and which version is answering"] = (
    "docksentry_build_info{version=" in _body)

# The two numbers the page is about. Four containers across two
# reachable hosts, two updates pending.
checks["it counts every container on every reachable host"] = (
    _gauge(_body, "docksentry_containers") == "4")
checks["…and every pending update"] = (
    _gauge(_body, "docksentry_updates_pending") == "2")

# Per host, including the one that did not answer — a host gauge that
# silently omits a dead machine is worse than no host gauge.
checks["a host that answered is reported up"] = (
    _gauge(_body, "docksentry_host_up", '{host="nas"}') == "1")
checks["…and one that did not is reported down, not left out"] = (
    _gauge(_body, "docksentry_host_up", '{host="pve1"}') == "0")
checks["…with each host's own container count"] = (
    _gauge(_body, "docksentry_host_containers", '{host="local"}') == "2"
    and _gauge(_body, "docksentry_host_containers", '{host="nas"}') == "2")
checks["…and its own pending count"] = (
    _gauge(_body, "docksentry_host_updates_pending", '{host="local"}') == "1"
    and _gauge(_body, "docksentry_host_updates_pending", '{host="nas"}') == "1")

# Per container, so an alert can name the thing that needs attention.
checks["each container with an update is named"] = (
    _gauge(_body, "docksentry_container_update_available",
           '{host="local",container="web"}') == "1"
    and _gauge(_body, "docksentry_container_update_available",
               '{host="nas",container="sonarr"}') == "1")
# …and one WITHOUT an update is not, or the metric says everything
# always needs updating.
checks["…and one without an update is not"] = (
    'container="db"' not in _body and 'container="jellyfin"' not in _body)

# Every metric carries its HELP and TYPE, which is what makes the
# endpoint readable by anything other than a human.
_named = [l.split("{")[0].split(" ")[0] for l in _body.splitlines()
          if l and not l.startswith("#")]
checks["every metric declares its HELP and TYPE"] = all(
    f"# HELP {m}" in _body and f"# TYPE {m}" in _body for m in set(_named))
# Not `_total`: Prometheus reserves that suffix for counters and these
# are gauges. promtool rejects the mismatch.
checks["…and no gauge pretends to be a counter"] = not any(
    m.endswith("_total") for m in _named)

# ── /api/status, the same numbers ────────────────────────────────────
_jstatus, _jheaders, _jbody = _serve("json")
checks["/api/status answers as JSON"] = (
    _jstatus == 200
    and _jheaders.get("content-type", "").startswith("application/json"))
_payload = json.loads(_jbody)


def _at(path, default=None):
    """One value out of the payload, or `default`.

    `.get`, not `[...]`: renaming a key used to make this file die with
    a KeyError, and a crash reports nothing at all — my own guard
    ratchet reads that as "could not say" rather than as a caught
    mutation. A missing key is a failed check, not a failed run.
    """
    cur = _payload
    for step in path:
        if isinstance(cur, dict):
            cur = cur.get(step, default)
        elif isinstance(cur, list) and isinstance(step, int) and step < len(cur):
            cur = cur[step]
        else:
            return default
    return cur


def _host(name):
    return next((h for h in _at(["hosts"], []) or []
                 if h.get("name") == name), {})


checks["…with the same totals as /metrics"] = (
    _at(["containers"]) == 4 and _at(["updates_pending"]) == 2)
checks["…and every host, reachable or not"] = (
    sorted(h.get("name") for h in _at(["hosts"], []) or [])
    == ["local", "nas", "pve1"])
checks["…saying which one did not answer"] = (
    _host("pve1").get("reachable") is False)
checks["…and naming the containers that have updates"] = (
    _host("nas").get("pending") == ["sonarr"])

# ── nothing to report is zero, not an error ──────────────────────────
# A fresh install scrapes too, and an endpoint that breaks on an empty
# answer is one somebody notices at the worst moment.
_s, _, _empty = _serve("metrics", views=[])
checks["an install with nothing to report still answers"] = (
    _s == 200 and _gauge(_empty, "docksentry_up") == "1")
checks["…with zeroes rather than missing lines"] = (
    _gauge(_empty, "docksentry_containers") == "0"
    and _gauge(_empty, "docksentry_updates_pending") == "0")

# ── a host whose name would break the format ─────────────────────────
# Label values are quoted; a name carrying a quote or a backslash must
# not be able to produce a line a scraper cannot parse.
_s, _, _odd = _serve("metrics", views=[
    {"host": 'we"ird\\host', "containers": [{"name": 'c"1'}],
     "pending": [{"name": 'c"1'}]}])
# Counting raw quotes was my first attempt and it was wrong, not the
# code: `\"` IS a correctly escaped quote and adds one to the count.
# The escaped pairs come out first, and what is left must be the
# delimiters — which is what a scraper's parser does.
def _delimiters_balanced(line):
    bare = line.replace("\\\\", "").replace('\\"', "")
    return bare.count('"') % 2 == 0


checks["a hostile host name cannot break the exposition"] = (
    _s == 200
    and all(_delimiters_balanced(l) for l in _odd.splitlines()
            if l and not l.startswith("#")))
checks["…and its quotes are escaped rather than dropped"] = (
    '\\"' in _odd and "\\\\" in _odd)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
