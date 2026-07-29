#!/usr/bin/env python3
"""Registry diagnostics in the update check (#53, @LeeNX).

The issue was a log that said "Up to date" and gave nothing to check that
claim against: two hashes truncated to 30 characters, no repository prefix,
no URL, no hint whether a mirror or proxy sat in the path.

Two things must hold at once here, and they pull in opposite directions:

  * the new output has to be genuinely useful, and
  * NONE of it may appear unless DEBUG is on — `_debug` prints
    unconditionally, so a new line written through it would grow the
    container log of every user who never asked for any of this.

The second one is the regression to guard, so it gets tested from both
sides. Registry, daemon and network are faked out — no sockets, no docker.
Exits non-zero on any failure.
"""
import sys, os, io, json, types, tempfile, contextlib, email.message
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import update_checker
from update_checker import UpdateChecker

FULL_LOCAL = "sha256:" + "66d80966792e621c9761c47919644198d35fd1c297e9a01e69ed3c1ae37db0c7"[:64]
FULL_REMOTE = "sha256:" + "968b93c034b6231be037b8abce159dedbf7eb16adbc79ee2b1555c0eea31a4d3"[:64]


class FakeResponse:
    """Just enough of an http.client.HTTPResponse for the logger."""

    def __init__(self, status=200, headers=None, url=None, body=b""):
        self.status = status
        self.url = url
        self._body = body
        self.headers = email.message.Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, headers=None):
    hdrs = email.message.Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError("https://x/", code, "Unauthorized", hdrs, None)


def make_checker(debug, outdated=False, remote_version="2.3.0"):
    """Fake single-container checker. Returns (checker, calls) where `calls`
    counts the remote metadata lookups — the expensive part that must stay
    off the full-sweep path."""
    tmp = tempfile.mkdtemp()
    cfg = types.SimpleNamespace(debug=debug,
                                pending_file=os.path.join(tmp, "pending.json"))
    chk = UpdateChecker(cfg)
    calls = []

    chk.get_running_containers = lambda: [
        {"name": "gitea-runner", "image": "gitea/runner:latest"}]
    chk._parse_image = lambda img: ("registry-1.docker.io", "gitea/runner", "latest")
    chk._get_local_digests = lambda img: [FULL_LOCAL]
    chk._get_local_repo_digests = lambda img: [f"gitea/runner@{FULL_LOCAL}"]
    chk._get_remote_digest = lambda r, repo, t: (FULL_REMOTE if outdated else FULL_LOCAL)
    chk._get_image_size = lambda img: "141 MB"
    chk._get_image_created = lambda img: "2026-07-11"
    chk._get_image_version_label = lambda img: ""
    chk._registry_environment = lambda: ("host linux/amd64, mirrors: none, "
                                         "daemon proxy: none, our proxy: none")

    def meta(registry, repository, tag):
        calls.append(tag)
        return {"version": remote_version, "created": "2026-07-11"}

    chk.get_remote_image_meta = meta
    return chk, calls


def run(chk, **kw):
    """check_all with stdout captured; returns everything that was printed."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        chk.check_all(**kw)
    return buf.getvalue()


def main():
    checks = {}

    # ── 1. THE gate: DEBUG off → not one new line ───────────────────
    # Every string below is something only the diagnostics emit. Any of
    # them showing up in a default run means the container log of a user
    # who never asked for this just started growing.
    chk, calls = make_checker(debug=False)
    out = run(chk)
    forbidden = ["Environment:", "HEAD https", "GET https", "auth ",
                 "content-type", "local image built", "is version",
                 "redirected to"]
    leaked = [f for f in forbidden if f in out]
    checks["gate: DEBUG off leaks no diagnostic line"] = not leaked
    if leaked:
        print("     leaked:", leaked)
    checks["gate: DEBUG off still prints the verdict"] = "Up to date" in out
    checks["gate: debug_log stays empty"] = chk.debug_log == []

    # _vdebug itself, directly — the helper is the whole gate
    quiet = UpdateChecker(types.SimpleNamespace(debug=False))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        quiet._vdebug("secret")
    checks["gate: _vdebug prints nothing without DEBUG"] = buf.getvalue() == ""
    loud = UpdateChecker(types.SimpleNamespace(debug=True))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        loud._vdebug("shown")
    checks["gate: _vdebug prints with DEBUG"] = "shown" in buf.getvalue()
    checks["gate: _vdebug feeds debug_log"] = loud.debug_log == ["shown"]

    # ── 2. digests: full length, repository prefix kept ─────────────
    chk, calls = make_checker(debug=True)
    out = run(chk)
    checks["digest: local printed in full"] = FULL_LOCAL in out
    checks["digest: local keeps repo prefix"] = f"gitea/runner@{FULL_LOCAL}" in out
    checks["digest: no 30-char truncation left"] = FULL_LOCAL[:30] + "..." not in out
    chk, calls = make_checker(debug=True, outdated=True)
    out = run(chk)
    checks["digest: remote printed in full"] = FULL_REMOTE in out

    # the bare-digest contract has to survive — has_selfupdate_available
    # and the comparison in check_all both rely on it
    real = UpdateChecker(types.SimpleNamespace(debug=False))
    real._repo_digest_cache = {"img": [f"gitea/runner@{FULL_LOCAL}"]}
    checks["digest: _get_local_digests still returns bare"] = (
        real._get_local_digests("img") == [FULL_LOCAL])

    # ── 3. environment line: mirrors + proxies, once per run ────────
    chk, calls = make_checker(debug=True)
    out = run(chk)
    checks["env: line present with DEBUG"] = "Environment: host linux/amd64" in out
    checks["env: printed once"] = out.count("Environment:") == 1

    UpdateChecker._host_platform_cache = ("linux", "arm64")
    UpdateChecker._daemon_net_cache = None
    calls_seen = []

    def fake_run(cmd, **kw):
        calls_seen.append(cmd)
        return types.SimpleNamespace(
            returncode=0,
            stdout='["https://mirror.local"]\thttp://proxy:3128\t\t.internal\n',
            stderr="")

    orig_run = update_checker.subprocess.run
    update_checker.subprocess.run = fake_run
    try:
        info = UpdateChecker._daemon_net_info()
        checks["env: mirror named"] = info["mirrors"] == "https://mirror.local"
        checks["env: daemon proxy named"] = (
            info["proxy"] == "http=http://proxy:3128, no_proxy=.internal")
        UpdateChecker._daemon_net_info()
        checks["env: docker info cached process-wide"] = len(calls_seen) == 1

        # no mirrors / no proxy reads as "none", not as empty space
        UpdateChecker._daemon_net_cache = None
        update_checker.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
            returncode=0, stdout="[]\t\t\t\n", stderr="")
        info = UpdateChecker._daemon_net_info()
        checks["env: no mirrors → none"] = info["mirrors"] == "none"
        checks["env: no daemon proxy → none"] = info["proxy"] == "none"

        # a socket proxy that forbids /info must not take the check down
        UpdateChecker._daemon_net_cache = None

        def boom(cmd, **kw):
            raise OSError("permission denied")

        update_checker.subprocess.run = boom
        info = UpdateChecker._daemon_net_info()
        checks["env: docker info failure → unknown"] = (
            info == {"mirrors": "unknown", "proxy": "unknown"})
    finally:
        update_checker.subprocess.run = orig_run
        UpdateChecker._daemon_net_cache = None
        UpdateChecker._host_platform_cache = None

    # our own proxy vars — urlopen reads them behind our back
    env_chk = UpdateChecker(types.SimpleNamespace(debug=True))
    saved = {k: os.environ.pop(k, None)
             for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                       "http_proxy", "https_proxy", "no_proxy")}
    try:
        checks["env: our proxy unset → none"] = env_chk._proxy_environment() == "none"
        os.environ["https_proxy"] = "http://bob:hunter2@proxy.lan:3128"
        got = env_chk._proxy_environment()
        checks["env: our proxy reported"] = got.startswith("https_proxy=http://")
        checks["env: proxy password masked"] = (
            "hunter2" not in got and "***@proxy.lan:3128" in got)
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    checks["env: mask leaves clean URLs alone"] = (
        UpdateChecker._mask_url("http://proxy.lan:3128") == "http://proxy.lan:3128")

    # ── 4. the HTTP exchange itself: URL, status, type, redirect ────
    dchk = UpdateChecker(types.SimpleNamespace(debug=True))
    seen = FakeResponse(
        status=200,
        headers={"Docker-Content-Digest": FULL_LOCAL,
                 "Content-Type": "application/vnd.oci.image.index.v1+json"},
        url="https://cdn.example.net/v2/gitea/runner/manifests/latest")
    orig_open = update_checker.urllib.request.urlopen
    update_checker.urllib.request.urlopen = lambda req, timeout=None: seen
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            digest = dchk._get_remote_digest("docker.io", "gitea/runner", "latest")
        out = buf.getvalue()
    finally:
        update_checker.urllib.request.urlopen = orig_open
    checks["http: digest still returned"] = digest == FULL_LOCAL
    checks["http: request URL logged"] = (
        "HEAD https://registry-1.docker.io/v2/gitea/runner/manifests/latest" in out)
    checks["http: status logged"] = "HTTP 200" in out
    checks["http: content-type logged"] = "image.index.v1+json" in out
    checks["http: redirect target logged"] = "cdn.example.net" in out
    checks["http: auth category logged"] = "auth anonymous" in out

    # ── 5. auth: category yes, token never ──────────────────────────
    tchk = UpdateChecker(types.SimpleNamespace(debug=True))
    tchk._get_docker_credentials = lambda registry: None
    token_body = json.dumps({"token": "SUPERSECRET", "expires_in": 300}).encode()
    negotiations = []

    def fake_open(req, timeout=None):
        negotiations.append(getattr(req, "full_url", req))
        return FakeResponse(body=token_body)

    challenge = ('Bearer realm="https://auth.docker.io/token",service="registry.docker.io"'
                 ',scope="repository:gitea/runner:pull"')
    update_checker.urllib.request.urlopen = fake_open
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tok = tchk._negotiate_token(challenge, "docker.io", "gitea/runner")
            tok2 = tchk._negotiate_token(challenge, "docker.io", "gitea/runner")
        out = buf.getvalue()
    finally:
        update_checker.urllib.request.urlopen = orig_open
    checks["auth: token returned"] = tok == "SUPERSECRET"
    checks["auth: token never printed"] = "SUPERSECRET" not in out
    checks["auth: category printed"] = "auth: bearer challenge" in out
    checks["auth: realm printed"] = "https://auth.docker.io/token" in out
    checks["auth: cached for the run (one negotiation)"] = (
        tok2 == "SUPERSECRET" and len(negotiations) == 1)
    checks["auth: rejected token is forgotten"] = (
        tchk._forget_token("docker.io", "gitea/runner") is None
        and tchk._token_cache == {})

    # credentials from config.json are a category of their own — and the
    # Basic-Auth header behind it stays out of the log
    cchk = UpdateChecker(types.SimpleNamespace(debug=True))
    cchk._get_docker_credentials = lambda registry: "dXNlcjpwYXNzd29yZA=="
    update_checker.urllib.request.urlopen = lambda req, timeout=None: FakeResponse(
        body=token_body)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cchk._negotiate_token(challenge, "private.example", "team/app")
        out = buf.getvalue()
    finally:
        update_checker.urllib.request.urlopen = orig_open
    checks["auth: config credentials named as such"] = (
        "auth: credentials from config" in out)
    checks["auth: basic header not printed"] = "dXNlcjpwYXNzd29yZA==" not in out

    # a private scope can carry an internal path — truncate it
    long_scope = "repository:" + "very-long-internal-group/" * 6 + "svc:pull"
    schk = UpdateChecker(types.SimpleNamespace(debug=True))
    schk._get_docker_credentials = lambda registry: None
    update_checker.urllib.request.urlopen = lambda req, timeout=None: FakeResponse(
        body=token_body)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            schk._negotiate_token(
                f'Bearer realm="https://r/token",scope="{long_scope}"', "r", "x")
        out = buf.getvalue()
    finally:
        update_checker.urllib.request.urlopen = orig_open
    checks["auth: long scope truncated"] = (long_scope not in out and "..." in out)

    # ── 6. version resolution: on demand only ───────────────────────
    chk, calls = make_checker(debug=False)
    run(chk)
    checks["resolve: full sweep without DEBUG resolves nothing"] = calls == []

    chk, calls = make_checker(debug=False)
    out = run(chk, only={"gitea-runner"})
    checks["resolve: single-container check resolves"] = calls == ["latest"]
    # The 🔍 button spends 2-3 registry GETs to answer this. If the answer
    # then only appeared under DEBUG we'd have paid Docker Hub's rate limit
    # for a line nobody sees — so an explicit single check prints it either
    # way. The full sweep keeps it behind DEBUG (asserted below).
    checks["resolve: single check prints the version without DEBUG"] = (
        "remote :latest is version 2.3.0" in out)
    checks["resolve: single check without DEBUG stays otherwise quiet"] = (
        "HEAD https://" not in out and "Environment:" not in out)

    chk, calls = make_checker(debug=True)
    out = run(chk, only={"gitea-runner"})
    checks["resolve: version named next to the verdict"] = (
        "remote :latest is version 2.3.0" in out)
    checks["resolve: build date named"] = "2026-07-11" in out
    checks["resolve: local size named"] = "141 MB" in out

    # an image without a version label must say so rather than stay silent
    chk, calls = make_checker(debug=True, remote_version="")
    out = run(chk, only={"gitea-runner"})
    checks["resolve: missing label reported"] = "carries no version label" in out

    # containers WITH an update keep the #44 behaviour, unchanged
    chk, calls = make_checker(debug=False, outdated=True)
    out = run(chk)
    checks["resolve: pending update still resolves (unchanged)"] = calls == ["latest"]
    checks["resolve: update verdict unchanged"] = "UPDATE AVAILABLE" in out

    # ── 7. _get_local_repo_digests against a faked docker ───────────
    rchk = UpdateChecker(types.SimpleNamespace(debug=False))
    update_checker.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
        returncode=0, stdout=json.dumps([f"gitea/runner@{FULL_LOCAL}", "broken"]),
        stderr="")
    try:
        got = rchk._get_local_repo_digests("gitea/runner:latest")
        checks["inspect: prefix kept, junk dropped"] = got == [f"gitea/runner@{FULL_LOCAL}"]
        update_checker.subprocess.run = lambda cmd, **kw: (_ for _ in ()).throw(
            OSError("no docker"))
        checks["inspect: cached, second call makes no call"] = (
            rchk._get_local_repo_digests("gitea/runner:latest")
            == [f"gitea/runner@{FULL_LOCAL}"])
        checks["inspect: missing docker → empty, no crash"] = (
            rchk._get_local_repo_digests("other:latest") == [])
    finally:
        update_checker.subprocess.run = orig_run

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
