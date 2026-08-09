#!/usr/bin/env python3
"""Four small things decided together, asserted together.

**An outcome count in the first line of the auto-update report.** The lines
below it already name every container, but a long report is split across
several Telegram messages and then "how did it go?" is spread over all of
them — which is how @LeeNX ended up with a screenshot showing "Auto-updating
3 container(s)…" and nothing after it (#56).

**A container claimed by `podman auto-update`.** The label
`io.containers.autoupdate` hands a container to Podman's own updater on a
systemd timer. Both it and Docksentry then have an opinion about that
container and whichever runs first wins. Same treatment as a quadlet (#55):
say so on the row, leave the decision alone. Free to check, because the
status row already carries its labels.

**Docker's own endpoints, at the moment one of ours fails.** `DOCKER_HOSTS`
is typed by hand and a typo is indistinguishable from a machine that is
down. Docker knows what it has been told about, so listing it costs one
command exactly when somebody is standing in front of "cannot reach nas".
Only on failure, and never as a second source of hosts — two places to keep
in step is how they drift.

**And the `default` context is not worth naming**, because it is the local
socket we are already using.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_engine import UpdateEngine  # noqa: E402

APP = os.path.join(os.path.dirname(__file__), "..", "app")


def src(name):
    return open(os.path.join(APP, name), encoding="utf-8").read()


def main():
    checks = {}

    # ── the outcome count ────────────────────────────────────────
    lines = ["✅ `a`: updated", "❌ `b`: rolled back", "⏸ `c`: major bump",
             "✅ `d`: updated", "⏭ `e`: skipped",
             "🔁 head rollback — dependents kicked: x"]
    c = UpdateEngine.count_results(lines)
    checks["every outcome glyph is counted"] = (
        c == {"updated": 2, "failed": 1, "held": 1, "skipped": 1})
    # The group note is about a group, not a container, and counting it
    # would make the numbers not add up against the list below.
    checks["the group note is not counted as a container"] = (
        sum(c.values()) == 5)
    checks["an empty batch counts nothing"] = UpdateEngine.count_results([]) == {}
    checks["…and so does None"] = UpdateEngine.count_results(None) == {}
    # Every glyph the engine actually emits must be in the table, or the
    # count silently under-reports.
    ue = src("update_engine.py")
    emitted = {g for g in ("✅", "❌", "⏸", "⏭") if f'"{g}' in ue or f"'{g}" in ue}
    checks["the glyph table covers what the engine emits"] = emitted <= set(
        UpdateEngine.RESULT_GLYPHS)

    # …and the bot puts it in the first line, not a fourth message.
    tb = src("telegram_bot.py")
    i = tb.index('self.t("autoupdate_done")')
    seg = tb[i - 600:i + 400]
    checks["the bot builds the count into the heading"] = (
        "count_results" in seg and "_head" in seg)

    # ── the podman auto-update badge ─────────────────────────────
    web = src("web_ui.py")
    i = web.index("io.containers.autoupdate")
    seg = web[i - 400:i + 700]
    checks["a claimed container is badged"] = 'badge badge-yellow' in seg
    checks["…from labels the row already has"] = 'c.get("labels")' in seg
    # Reporting, not acting. Nothing about this label may skip, pin or
    # otherwise change what Docksentry does — that is the user's call and
    # the whole difference between this and monitor-only.
    # Once in the comment that explains it, once where it is read, and
    # nowhere in the update core — this reports, it does not act. That
    # is the whole difference between it and monitor-only.
    code = "\n".join(l for l in web.splitlines() if not l.strip().startswith("#"))
    checks["…and nothing else keys off that label"] = (
        code.count("io.containers.autoupdate") == 1
        and "io.containers.autoupdate" not in src("update_checker.py")
        and "io.containers.autoupdate" not in src("update_engine.py"))

    # ── the context hint ─────────────────────────────────────────
    i = web.index("_CONTEXT_FORMATS")
    seg = web[i:web.index("def _ctx_hint")]
    # Both CLIs keep a context list and neither accepts the other's field
    # name. Measured: podman rejects `.DockerEndpoint` with exit 125, and
    # docker rejects `.URI` with exit **0** and an error in the output —
    # so the first version, which went by exit code alone, produced
    # nothing on Podman and would have handed an error string back as an
    # endpoint on Docker.
    checks["both CLIs' endpoint fields are tried"] = (
        "{{.Name}}|{{.DockerEndpoint}}" in seg and "{{.Name}}|{{.URI}}" in seg)
    checks["…and a failed template is caught by its text, not its exit code"] = (
        "can't evaluate field" in seg)
    checks["…exit code still counts too"] = "returncode" in seg
    checks["…best-effort, never raised"] = "except Exception" in seg
    # A half-parsed line ("name" with no endpoint) is not an answer.
    checks["…and a line without an endpoint is dropped"] = (
        "endpoint.strip()" in seg and "sep and" in seg)

    i = web.index("def _ctx_hint")
    seg = web[i:i + 900]
    checks["the hint skips the default context"] = 'n != "default"' in seg
    checks["…and the endpoint that just failed"] = 'view.get("endpoint")' in seg
    checks["…and says nothing when there is nothing to say"] = 'return ""' in seg
    # Only on failure. If this ever became a source of hosts there would
    # be two places to keep in step, which is how they drift apart.
    checks["contexts are only consulted for an unreachable host"] = (
        web.count("_docker_contexts()") == 1
        and '"unreachable"' in web[web.index("_docker_contexts()") - 400:
                                   web.index("_docker_contexts()") + 100])

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
