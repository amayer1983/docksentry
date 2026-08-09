#!/usr/bin/env python3
"""Web UI actions on several hosts (#7).

Until now the status table LISTED every managed host but only ever acted on
the local one — remote rows deliberately rendered a muted "—" where the
buttons belong, because /api/update, /api/pin, /api/lifecycle & co. all ran
against the local backend and a button there would have done the right
thing to the wrong machine.

This file pins down the rules that let those buttons exist:

  * the `name` field of every action POST is a HOST KEY
    (`container_store.host_key`): `nginx` locally, `nas/nginx` remotely —
    the same identifier the Telegram callbacks carry, not a second format.
  * a request WITHOUT a host in it is the local host. Always. That is what
    bookmarked POSTs and single-host forms send.
  * a request naming a host this instance does not manage does NOTHING.
    It never falls back to local — an action on the wrong machine is the
    one failure mode this feature must not have.
  * per-host isolation: pinning `web` on `nas` leaves the local `web`
    alone, and vice versa.
  * a SINGLE-host install renders byte-identically with and without a
    registry, carries no host in any form field, and gets no filter.

Real `ContainerStore` on a temp dir (so the per-host key isolation is
tested against the actual files), fake backends/checkers, fake bot. No
Docker, no network, no sockets. Exits non-zero on any failure.
"""
import io
import json
import os
import sys
import tempfile
import threading
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_ui                                            # noqa: E402
from hosts import HostRegistry, ManagedHost              # noqa: E402
from container_store import ContainerStore, HostScopedStore, host_key  # noqa: E402

checks = {}


# ── Fakes ─────────────────────────────────────────────────────────────

class CP:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _inspect_json(name):
    return {"Name": "/" + name,
            "Config": {"Image": f"reg/{name}:1", "Labels": {}},
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
            "Image": "sha256:" + "a" * 64}


class FakeBackend:
    """Answers ps/inspect for a fixed container list. `dead=True` makes
    every call raise, which is how an unreachable endpoint behaves once
    the CLI times out."""

    def __init__(self, name, names, dead=False, error=None):
        self.name, self.names, self.dead = name, list(names), dead
        self.error = error or "unreachable"
        self.calls = []

    def _guard(self):
        if self.dead:
            raise OSError(self.error)

    def ps(self, **kw):
        self.calls.append(("ps", kw))
        self._guard()
        if kw.get("quiet"):
            return CP("\n".join("id-" + n for n in self.names))
        return CP("\n".join(self.names))

    def inspect(self, refs, **kw):
        self.calls.append(("inspect", refs))
        self._guard()
        refs = [refs] if isinstance(refs, str) else list(refs)
        return CP(json.dumps(
            [_inspect_json(r[3:] if r.startswith("id-") else r) for r in refs]))

    def image_inspect(self, images, **kw):
        self.calls.append(("image_inspect", images))
        self._guard()
        return CP("[]")

    def run(self, args, **kw):
        self.calls.append(tuple(args))
        self._guard()
        return CP("")


class FakeChecker:
    def __init__(self, name, own=""):
        self.name, self._own = name, own
        self.checked = []
        self.updated = []
        self.debug_log = []

    def _own_container_name(self):
        return self._own

    def get_disk_usage(self):
        return 10.0, 1 << 30, 2 << 30

    def check_all(self, bot=None, only=None):
        self.checked.append(sorted(only or []))
        return []

    def _would_kill_self(self, name):
        return bool(self._own) and name == self._own

    def update_container(self, name, image, **kw):
        self.updated.append((name, image))
        return True, "updated"


class FakeEngine:
    def __init__(self):
        self._update_lock = threading.Lock()
        self.batches = []

    def _process_update_batch(self, updates, checker, *, auto):
        # Record WHICH checker recreated WHICH containers — the whole
        # point of the routing under test.
        assert self._update_lock.locked(), "batch ran without the update lock"
        self.batches.append((checker.name,
                             [(u.get("host"), u["name"]) for u in updates]))
        return ([f"ok {u['name']}" for u in updates], len(updates), [])


class FakeBot:
    enabled = False
    update_running = False
    notifier = None

    def __init__(self):
        self.engine = FakeEngine()
        self.removed = []
        self.lifecycle = []
        self.major = []

    def _remove_from_pending(self, keys):
        self.removed.extend(keys)

    def _run_queued_selfupdate(self):
        pass

    def _lifecycle_action(self, action, name, checker, backend=None, host=None):
        self.lifecycle.append((action, name, checker.name,
                               getattr(backend, "name", None), host))
        return True, "ok"

    def _confirm_major_update(self, checker, container_key):
        self.major.append(container_key)


# ── Harness ───────────────────────────────────────────────────────────

def make_config(tmp):
    def p(n):
        return os.path.join(tmp, n)
    return types.SimpleNamespace(
        language="en", auto_selfupdate=False, ui_mode="advanced", debug=False,
        disk_warn_percent=85, web_password="", web_setup_done=True,
        pending_file=p("pending.json"), history_file=p("history.json"),
        maintenance_file=p("maintenance.json"),
        pinned_file=p("pinned.json"), autoupdate_file=p("autoupdate.json"),
        update_windows_file=p("windows.json"),
        ask_before_major_file=p("askmajor.json"),
        trust_running_file=p("trust.json"), cooldown_file=p("cooldown.json"),
        protect_stop_file=p("protect.json"),
        major_pending_file=p("major.json"), groups_file=p("groups.json"),
        notes_file=p("notes.json"), links_file=p("links.json"),
    )


def handler_for(config, store, hosts, local_backend, local_checker, bot):
    hc = web_ui.create_handler(config, local_checker, bot, store,
                               backend=local_backend, hosts=hosts)
    h = hc.__new__(hc)
    h.path = "/"
    h.rendered = []
    h.redirects = []
    h._send_html = lambda html, status=200: h.rendered.append(html)
    h._send_redirect = lambda path="/": h.redirects.append(path)
    return h


def post(h, path, body):
    """Drive one POST through the real do_POST dispatch."""
    h.path = path
    h.headers = {"Content-Length": str(len(body)), "Host": "ds.local",
                 "Origin": "http://ds.local"}
    h.rfile = io.BytesIO(body.encode())
    h.wfile = io.BytesIO()
    h.send_response = lambda *a, **k: None
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda *a, **k: None
    h.do_POST()


def render(h):
    h.rendered = []
    h.path = "/"
    h._page_status()
    return h.rendered[-1] if h.rendered else ""


#: What podman 4.9.3 actually writes when it reaches an ssh host and the
#: key is refused — copied from a run against a local sshd whose
#: authorized_keys held a different key. Two lines, and the useful one is
#: the SECOND: line one sends the reader to `podman machine`, which has
#: nothing to do with a refused key. The Web UI used to print neither and
#: say only "dead: unreachable", which fits a refused key, a closed port,
#: a DNS typo and a wrong socket path equally badly.
PODMAN_SSH_ERROR = (
    "Cannot connect to Podman. Please verify your connection to the Linux "
    "system using `podman system connection list`, or try `podman machine "
    "init` and `podman machine start` to manage a new Linux VM\n"
    "Error: unable to connect to Podman socket: failed to connect: ssh: "
    "handshake failed: ssh: unable to authenticate, attempted methods "
    "[none publickey], no supported methods remain: ssh://root@dead/run/"
    "podman/podman.sock"
)


def build(tmp, multi=True, pending=()):
    """local(web, db) + nas(web, plex) + an unreachable `dead`."""
    config = make_config(tmp)
    store = ContainerStore(config)
    with open(config.pending_file, "w") as f:
        json.dump(list(pending), f)
    lb, lc = FakeBackend("local", ["web", "db"]), FakeChecker("local")
    nb, nc = FakeBackend("nas", ["web", "plex"]), FakeChecker("nas")
    entries = [ManagedHost("local", lb, lc, HostScopedStore(store, "local"),
                           is_local=True)]
    if multi:
        entries.append(ManagedHost("nas", nb, nc, HostScopedStore(store, "nas"),
                                   endpoint="ssh://nas"))
        entries.append(ManagedHost(
            "dead", FakeBackend("dead", [], dead=True, error=PODMAN_SSH_ERROR),
            FakeChecker("dead"),
            HostScopedStore(store, "dead"), endpoint="ssh://dead"))
    hosts = HostRegistry(entries)
    bot = FakeBot()
    h = handler_for(config, store, hosts, lb, lc, bot)
    return types.SimpleNamespace(h=h, config=config, store=store, bot=bot,
                                 hosts=hosts, lb=lb, lc=lc, nb=nb, nc=nc)


def row_hosts(html):
    """{container name: host} read back off the rendered table."""
    import re
    body = html.split('<tbody id="ctblBody">')[1].split("</tbody>")[0]
    out = {}
    for chunk in body.split("<tr")[1:]:
        m = re.search(r'class="bulk-cb" value="([^"]*)"', chunk)
        hm = re.search(r'data-host="([^"]*)"', chunk)
        if m:
            out[m.group(1)] = hm.group(1) if hm else None
    return out


# ── 1. single-host: unchanged, in every visible respect ───────────────

tmp = tempfile.mkdtemp()
PENDING = [{"name": "web", "image": "reg/web:1", "host": "local"},
           {"name": "web", "image": "reg/web:9", "host": "nas"},
           {"name": "plex", "image": "reg/plex:9", "host": "nas"}]

one = build(tempfile.mkdtemp(), multi=False, pending=PENDING)
one_html = render(one.h)

# The exact same config/store rendered with NO registry at all — what an
# embedder or a render test builds.
noreg_cfg = make_config(tempfile.mkdtemp())
noreg_store = ContainerStore(noreg_cfg)
with open(noreg_cfg.pending_file, "w") as f:
    json.dump(PENDING, f)
noreg_h = handler_for(noreg_cfg, noreg_store, None,
                      FakeBackend("local", ["web", "db"]),
                      FakeChecker("local"), FakeBot())
noreg_html = render(noreg_h)

checks["single host: a one-host registry renders byte-identically to none"] = (
    one_html == noreg_html)
checks["single host: no host column"] = "<th>Host</th>" not in one_html
checks["single host: no host filter"] = 'id="hostFilter"' not in one_html

def table_only(html):
    """Just the container TABLE, without the card list beside it.

    The status page renders every container twice — once as a table row and
    once as a card for narrow screens, both from the same locals so they
    cannot disagree about state. Any assertion that COUNTS occurrences has
    to say which of the two it means, or it silently doubles.
    """
    start = html.find('<table id="ctbl"')
    end = html.find("</table>", start)
    return html[start:end] if start >= 0 else html


checks["single host: no data-host on any row"] = "data-host=" not in one_html
import re as _re  # noqa: E402
_one_table = table_only(one_html)
_form_values = set(_re.findall(r'name="name" value="([^"]*)"', _one_table))
_cb_values = set(_re.findall(r'class="bulk-cb" value="([^"]*)"', _one_table))
checks["single host: form fields carry the bare container name"] = (
    _form_values == {"web", "db"} and _cb_values == {"web", "db"})
checks["single host: only the LOCAL pending entry lights a row up"] = (
    _one_table.count("/api/update") == 1)

# ── 2. multi-host rendering: rows, hosts, actions ─────────────────────

m = build(tmp, pending=PENDING)
html = render(m.h)
hosts_by_key = row_hosts(html)

checks["multi: every host's containers are listed"] = (
    hosts_by_key == {"web": "local", "db": "local",
                     "nas/web": "nas", "nas/plex": "nas"})
checks["multi: the host column is back"] = "<th>Host</th>" in html
checks["multi: the host filter offers every host plus 'all'"] = (
    'id="hostFilter"' in html
    and '<option value="">All hosts</option>' in html
    and '<option value="nas">nas</option>' in html
    and '<option value="dead">dead</option>' in html)
checks["multi: an unreachable host is one line, not a broken page"] = (
    "dead: unreachable" in html and 'class="host-unreachable"' in html)
# …and that line now carries WHY. "unreachable" alone was the same word
# for a refused SSH key, a closed port and a DNS typo; only the CLI's own
# text tells them apart, and Telegram/Discord have quoted it all along.
_dead_row = [r for r in html.split("<tr") if 'data-host="dead"' in r]
checks["multi: the unreachable row quotes what the CLI said"] = (
    len(_dead_row) == 1
    and "Reason:" in _dead_row[0]
    and "unable to authenticate" in _dead_row[0])
# The last line only. Podman's first line points at `podman machine`,
# which is never the answer for a remote host, and printing it would send
# the reader somewhere unrelated — the exact failure this row is for.
checks["multi: and drops podman's `podman machine` boilerplate"] = (
    "podman machine" not in _dead_row[0])

_nas_web = [c for c in table_only(html).split("<tr") if 'value="nas/web"' in c]
checks["multi: a remote row has the same actions as a local one"] = (
    len(_nas_web) == 1
    and all(a in _nas_web[0] for a in ("/api/update", "/api/pin",
                                       "/api/autoupdate", "/api/lifecycle",
                                       "/api/ask_major", "dsCheckOne"))
    and '<td class="actions-cell"><span class="muted">—</span></td>'
        not in _nas_web[0])
checks["multi: every form on a remote row carries that host's key"] = (
    _nas_web[0].count('name="name" value="nas/web"') >= 5
    and 'name="name" value="web"' not in _nas_web[0])
checks["multi: a remote row's update button follows that host's pending"] = (
    '/api/update' in _nas_web[0])
_local_db = [c for c in html.split("<tr") if 'value="db"' in c][0]
checks["multi: a local row with no pending update has no update button"] = (
    "/api/update" not in _local_db)

# ── 3. target resolution ──────────────────────────────────────────────

from urllib.parse import parse_qs  # noqa: E402


def target(h, key):
    return h._action_target(parse_qs(f"name={key}") if key else {})


t_local = target(m.h, "web")
t_nas = target(m.h, "nas/web")
checks["no host in the request resolves to the local objects"] = (
    t_local is not None and t_local[0] == "local" and t_local[1] == "web"
    and t_local[2] is m.lb and t_local[3] is m.lc)
checks["an explicit `local` resolves the same way"] = (
    target(m.h, "local/web")[:2] == ("local", "web"))
checks["a remote key resolves that host's backend and checker"] = (
    t_nas is not None and t_nas[0] == "nas" and t_nas[1] == "web"
    and t_nas[2] is m.nb and t_nas[3] is m.nc)
checks["a remote key resolves that host's store view"] = (
    t_nas[4] is m.hosts.get("nas").store)
checks["an unmanaged host resolves to nothing — never to local"] = (
    target(m.h, "typo/web") is None)
checks["an empty name resolves to nothing"] = (
    target(m.h, "") is None and target(m.h, "nas/") is None)

single = build(tempfile.mkdtemp(), multi=False)
checks["single host: a remote key is refused, not taken as local"] = (
    target(single.h, "nas/web") is None)

# ── 4. pin isolation through the real POST path ───────────────────────

m2 = build(tempfile.mkdtemp(), pending=PENDING)
post(m2.h, "/api/pin", "name=nas%2Fweb")
checks["pin on a remote host writes that host's key"] = (
    m2.store.get_pinned() == ["nas/web"])
checks["pin on host A does not pin on host B"] = (
    not HostScopedStore(m2.store, "local").is_pinned("web")
    and HostScopedStore(m2.store, "nas").is_pinned("web"))

post(m2.h, "/api/pin", "name=web")
checks["pin with no host given pins locally"] = (
    sorted(m2.store.get_pinned()) == ["nas/web", "web"])
post(m2.h, "/api/unpin", "name=nas%2Fweb")
checks["unpin on a remote host leaves the local pin alone"] = (
    m2.store.get_pinned() == ["web"])

post(m2.h, "/api/pin", "name=typo%2Fweb")
checks["pin naming an unmanaged host changes nothing"] = (
    m2.store.get_pinned() == ["web"])

# auto-update, ask-major, trust, protect, cooldown, note, link: same rule
post(m2.h, "/api/autoupdate", "name=nas%2Fplex")
post(m2.h, "/api/ask_major", "name=nas%2Fplex")
post(m2.h, "/api/trust_running", "name=nas%2Fplex")
post(m2.h, "/api/protect", "name=nas%2Fplex")
post(m2.h, "/api/cooldown", "name=nas%2Fplex&seconds=42")
post(m2.h, "/api/note", "name=nas%2Fplex&note=hi")
post(m2.h, "/api/link", "name=nas%2Fplex&url=https%3A%2F%2Fexample.com%2Fx")
_nas = HostScopedStore(m2.store, "nas")
_loc = HostScopedStore(m2.store, "local")
checks["every per-container toggle lands on the named host only"] = (
    _nas.is_auto("plex") and not _loc.is_auto("plex")
    and _nas.is_ask_before_major("plex") and not _loc.is_ask_before_major("plex")
    and _nas.is_trust_running("plex") and not _loc.is_trust_running("plex")
    and _nas.is_protect_stop("plex") and not _loc.is_protect_stop("plex")
    and _nas.get_cooldown("plex") == 42 and _loc.get_cooldown("plex") == 0
    and _nas.get_note("plex") == "hi" and _loc.get_note("plex") == ""
    and _nas.get_link("plex") and not _loc.get_link("plex"))
checks["a remote per-container form returns to the table, not to a "
       "local detail URL"] = all(
    not r.startswith("/container/") for r in m2.h.redirects)

# ── 5. lifecycle routes to the right host ─────────────────────────────

m3 = build(tempfile.mkdtemp())
post(m3.h, "/api/lifecycle", "name=nas%2Fplex&action=restart")
checks["lifecycle on a remote row uses that host's checker AND backend"] = (
    m3.bot.lifecycle == [("restart", "plex", "nas", "nas", "nas")])
post(m3.h, "/api/lifecycle", "name=db&action=restart")
checks["lifecycle with no host given stays local"] = (
    m3.bot.lifecycle[-1] == ("restart", "db", "local", "local", "local"))
post(m3.h, "/api/lifecycle", "name=typo%2Fdb&action=restart")
checks["lifecycle naming an unmanaged host does nothing"] = (
    len(m3.bot.lifecycle) == 2)

# The self-kill guard is about THIS process, which is local by definition.
m4 = build(tempfile.mkdtemp())
m4.lc._own = "web"
m4.nc._own = ""
post(m4.h, "/api/lifecycle", "name=web&action=stop")
checks["the self-kill guard still refuses a local stop of ourselves"] = (
    m4.bot.lifecycle == [])
post(m4.h, "/api/lifecycle", "name=nas%2Fweb&action=stop")
checks["the self-kill guard does not refuse a same-named remote container"] = (
    m4.bot.lifecycle == [("stop", "web", "nas", "nas", "nas")])

# ── 6. updates: right pending entries, right checker, one lock ────────

m5 = build(tempfile.mkdtemp(), pending=PENDING)
post(m5.h, "/api/update", "name=nas%2Fweb")
for _ in range(50):
    if m5.bot.engine.batches:
        break
    import time
    time.sleep(0.02)
checks["update on a remote row runs that host's entry through its checker"] = (
    m5.bot.engine.batches == [("nas", [("nas", "web")])])
checks["update on a remote row drops only that host's pending entry"] = (
    m5.bot.removed == [("nas", "web")])

m6 = build(tempfile.mkdtemp(), pending=PENDING)
m6.h._run_web_update_batch(["web"])
checks["update with no host given uses the local entry and local checker"] = (
    m6.bot.engine.batches == [("local", [("local", "web")])])

m7 = build(tempfile.mkdtemp(), pending=PENDING)
m7.h._run_web_update_batch(["web", "nas/plex"])
checks["a mixed batch is one call per host, each with its own checker"] = (
    m7.bot.engine.batches == [("local", [("local", "web")]),
                              ("nas", [("nas", "plex")])])
checks["a mixed batch removes both hosts' entries, keyed by host"] = (
    sorted(m7.bot.removed) == [("local", "web"), ("nas", "plex")])
checks["the update lock is released again afterwards"] = (
    not m7.bot.engine._update_lock.locked())

m8 = build(tempfile.mkdtemp(), pending=PENDING)
m8.h._run_web_update_batch(["typo/web"])
checks["an update naming an unmanaged host runs nothing at all"] = (
    m8.bot.engine.batches == [] and m8.bot.removed == [])

m9 = build(tempfile.mkdtemp(), pending=PENDING)
m9.bot.engine._update_lock.acquire()
m9.h._run_web_update_batch(["nas/web"])
checks["a busy update lock blocks the whole batch, not just one host"] = (
    m9.bot.engine.batches == [])
m9.bot.engine._update_lock.release()

# ── 7. bulk actions across hosts ──────────────────────────────────────

m10 = build(tempfile.mkdtemp(), pending=PENDING)
post(m10.h, "/api/bulk", "action=pin&names=web%2Cnas%2Fplex")
for _ in range(50):
    if m10.store.get_pinned():
        break
    import time
    time.sleep(0.02)
checks["bulk pin splits a mixed selection per host"] = (
    sorted(m10.store.get_pinned()) == ["nas/plex", "web"])

m11 = build(tempfile.mkdtemp(), pending=PENDING)
m11.h._api_bulk("autoupdate_on", ["nas/web", "typo/x"])
checks["bulk auto-update-on lands on the named host only"] = (
    m11.store.get_autoupdate() == ["nas/web"])
m11.h._api_bulk("autoupdate_off", ["nas/web"])
checks["bulk auto-update-off removes only that host's entry"] = (
    m11.store.get_autoupdate() == [])

# ── 8. per-container check runs on the named host ─────────────────────

m12 = build(tempfile.mkdtemp())
r = m12.h._api_check_one("plex", m12.nc)
checks["check-one on a remote row uses that host's checker"] = (
    r["ok"] and m12.nc.checked == [["plex"]] and m12.lc.checked == [])
m12.h._api_check_one("db")
checks["check-one with no checker given uses the local one"] = (
    m12.lc.checked == [["db"]])

# ── 9. deferred major updates are resumed on their own host ───────────

m13 = build(tempfile.mkdtemp())
HostScopedStore(m13.store, "nas").add_pending_major(
    "plex", {"image": "reg/plex:2", "old_version": "1", "new_version": "2"})
HostScopedStore(m13.store, "local").add_pending_major(
    "plex", {"image": "reg/plex:2", "old_version": "1", "new_version": "2"})
html13 = render(m13.h)
checks["a deferred major shows up once per host, with its host key"] = (
    'name="name" value="nas/plex"' in html13
    and 'name="name" value="plex"' in html13
    and html13.count("/api/major_confirm") == 4)
post(m13.h, "/api/major_confirm", "name=nas%2Fplex&action=reject")
checks["rejecting a remote major clears that host's entry only"] = (
    "plex" in m13.store.get_pending_major()
    and "nas/plex" not in m13.store.get_pending_major())
post(m13.h, "/api/major_confirm", "name=nas%2Fplex&action=confirm")
for _ in range(50):
    if m13.bot.major:
        break
    import time
    time.sleep(0.02)
checks["confirming a major hands the host key to the shared resumer"] = (
    m13.bot.major == ["nas/plex"])

# ── 10. host_key is the one identifier, not a second format ───────────

checks["the rendered keys ARE container_store.host_key output"] = (
    host_key("local", "web") == "web" and host_key("nas", "web") == "nas/web"
    and set(row_hosts(html)) == {host_key("local", "web"),
                                 host_key("local", "db"),
                                 host_key("nas", "web"),
                                 host_key("nas", "plex")})


def main():
    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
