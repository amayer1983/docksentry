#!/usr/bin/env python3
"""Scoped update check — `UpdateChecker.check_all(only={...})`.

Backs the per-container check button in the Web UI. The dangerous part is
the pending file: a scoped run must NOT rewrite pending_updates.json
wholesale, or every container it didn't look at loses its pending entry —
and with it its update badge and update button.

Docker and the registry are faked out — no network, no daemon. Exits
non-zero on any failure.
"""
import sys, os, io, json, types, tempfile, contextlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from update_checker import UpdateChecker


def make_checker(names, outdated, debug=False):
    """Fake checker over `names` (repository == container name); the ones in
    `outdated` report a differing remote digest. Returns
    (checker, pending_file, calls) — `calls` records every remote lookup."""
    tmp = tempfile.mkdtemp()
    pending_file = os.path.join(tmp, "pending.json")
    cfg = types.SimpleNamespace(debug=debug, pending_file=pending_file)
    chk = UpdateChecker(cfg)
    calls = []

    chk.get_running_containers = lambda: [{"name": n, "image": f"reg/{n}:tag"} for n in names]
    chk._parse_image = lambda img: ("reg", img.split("/", 1)[1].split(":")[0], "tag")
    chk._get_local_digests = lambda img: ["sha256:LOCAL"]

    def remote(registry, repository, tag):
        calls.append(repository)
        return "sha256:REMOTE" if repository in outdated else "sha256:LOCAL"

    chk._get_remote_digest = remote
    chk._get_image_size = lambda img: 0
    chk._get_image_created = lambda img: ""
    chk._get_image_version_label = lambda img: ""
    chk._parse_semver = lambda t: None
    chk.get_remote_image_meta = lambda *a, **k: {}
    return chk, pending_file, calls


def seed(pending_file, entries):
    with open(pending_file, "w") as f:
        json.dump(entries, f)


def load(pending_file):
    if not os.path.exists(pending_file):
        return None
    with open(pending_file) as f:
        return json.load(f)


def by_name(entries):
    return {u["name"]: u for u in (entries or [])}


def main():
    checks = {}
    quiet = contextlib.redirect_stdout(io.StringIO())

    # ── 1. only={"a"} really checks only a ──────────────────────────
    chk, pf, calls = make_checker(["a", "b", "c"], outdated={"a"})
    with quiet:
        res = chk.check_all(only={"a"})
    checks["scoped: exactly one remote lookup"] = calls == ["a"]
    checks["scoped: returns a list"] = isinstance(res, list)
    checks["scoped: reports a's update"] = [u["name"] for u in res] == ["a"]

    # ── 2. THE regression: other containers' entries survive ────────
    chk, pf, calls = make_checker(["a", "b"], outdated={"a"})
    b_entry = {"name": "b", "image": "reg/b:tag", "size": 42, "created": "2026-01-01"}
    seed(pf, [{"name": "a", "image": "reg/a:tag"}, b_entry])
    with quiet:
        chk.check_all(only={"a"})
    after = by_name(load(pf))
    checks["merge: b's entry untouched"] = after.get("b") == b_entry
    checks["merge: a still pending"] = "a" in after
    checks["merge: b not re-checked"] = calls == ["a"]

    # ── 3. checked container that became up to date drops out ───────
    chk, pf, calls = make_checker(["a", "b"], outdated=set())
    seed(pf, [{"name": "a", "image": "reg/a:tag"}, b_entry])
    with quiet:
        res = chk.check_all(only={"a"})
    after = by_name(load(pf))
    checks["up to date: empty return"] = res == []
    checks["up to date: a removed from pending"] = "a" not in after
    checks["up to date: b still there"] = after.get("b") == b_entry

    # ── 4. unknown name → nothing checked, nothing touched ──────────
    chk, pf, calls = make_checker(["a", "b"], outdated={"a"})
    seeded = [{"name": "a", "image": "reg/a:tag"}, b_entry]
    seed(pf, seeded)
    with quiet:
        res = chk.check_all(only={"nope"})
    checks["unknown name: empty return"] = res == []
    checks["unknown name: no remote lookups"] = calls == []
    checks["unknown name: pending unchanged"] = load(pf) == seeded

    # ── 5. only=None keeps the old full-overwrite behaviour ─────────
    chk, pf, calls = make_checker(["a", "b"], outdated={"a"})
    seed(pf, [{"name": "ghost", "image": "reg/ghost:tag"}, b_entry])
    with quiet:
        res = chk.check_all()
    after = load(pf)
    checks["full run: checks everything"] = sorted(calls) == ["a", "b"]
    checks["full run: returns a's update"] = [u["name"] for u in res] == ["a"]
    checks["full run: file overwritten wholesale"] = [u["name"] for u in after] == ["a"]

    # ── 6. missing / corrupt pending file is survivable ─────────────
    chk, pf, calls = make_checker(["a"], outdated={"a"})
    with quiet:
        chk.check_all(only={"a"})          # file does not exist yet
    checks["missing file: created with the update"] = [u["name"] for u in load(pf)] == ["a"]

    chk, pf, calls = make_checker(["a"], outdated={"a"})
    with open(pf, "w") as f:
        f.write("{not json")
    with quiet:
        chk.check_all(only={"a"})
    checks["corrupt file: treated as empty"] = [u["name"] for u in load(pf)] == ["a"]

    # ── 7. debug buffer holds only this run's lines ─────────────────
    chk, pf, calls = make_checker(["a", "b"], outdated={"a"}, debug=True)
    with quiet:
        chk.check_all()
        chk.check_all(only={"a"})
    log = "\n".join(chk.debug_log)
    checks["debug log: reset per run (no trace of b)"] = "reg/b" not in log
    checks["debug log: mentions the scoped container"] = "reg/a" in log

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
