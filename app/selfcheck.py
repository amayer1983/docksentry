"""What Docksentry can see of itself, so nobody has to run a shell for it.

On 20.09. I asked @NotRetarded twice for `docker exec … ls -la` to find
out whether a mount of his covered a compose path. The first one failed
because I had typed my own container's name; the second worked and told
me what Docksentry already knew. He is not the first — every Compose
mount question in #2 and #65 has ended in somebody pasting shell output
back at me.

There is nothing here the program did not already know. The container
name, its own mounts, and whether a given compose file can be opened are
all facts it reads anyway, scattered across three modules and readable
in none of them. This gathers them and says them.

Two rules, both from `hostdiag`, whose job this is a cousin of:

  * Everything is **measured**, never inferred from the shape of an
    error. "Not reachable" means we tried to open it.
  * A fact we could not establish is reported as unknown rather than
    guessed. An empty container name is "I could not tell", not "none".
"""
import os

import compose_paths


def _first_path(files, raw):
    """One path to show for a container, out of what the label held.

    A label may name several files, and Docker joins them with commas
    that a path is also allowed to contain. Nothing here depends on
    getting that right — it decides what to print and which mount to
    look for, not what to deploy from — so the first piece that starts
    at the root wins, and an unsplittable label is shown whole.
    """
    if files:
        first = files[0]
        if "," in first:
            for piece in first.split(","):
                if piece.startswith("/"):
                    return piece
        return first
    if raw and "," in raw:
        for piece in raw.split(","):
            if piece.startswith("/"):
                return piece
    return raw


def collect(checker, names):
    """Gather the facts. Returns the dict `render` expects.

    `checker` is an UpdateChecker — it already knows our own container
    name and our own mounts, and can read a container's compose labels.
    `names` are the containers to look at; a caller with a shorter list
    gets a shorter answer.
    """
    own = ""
    mounts = []
    try:
        own = checker._own_container_name() or ""
        mounts = checker._own_mounts()
    except Exception:                                   # noqa: BLE001
        pass

    rows = []
    for name in names or []:
        try:
            info = checker._get_compose_info(name)
        except Exception:                               # noqa: BLE001
            continue
        if not info:
            continue                                    # not a compose container
        cfg = info.get("compose_file") or ""
        files = []
        try:
            files = checker._compose_files(cfg, info.get("compose_dir"))
        except Exception:                               # noqa: BLE001
            files = [cfg] if cfg else []
        # `_compose_files` hands back the raw label untouched when it
        # cannot verify a comma split — right for the update path, where
        # deploying from the wrong file is worse than not deploying. For
        # a report it leaves `/a/one.yml,/a/two.yml` on screen as if it
        # were one path. We are not deploying from it, so split for
        # display and judge the first piece that looks like a path.
        shown = _first_path(files, cfg)
        rows.append({
            "container": name,
            "path": shown,
            # Measured, not deduced: we ask the filesystem.
            "readable": bool(files) and all(os.path.isfile(f) for f in files),
            "covered_by": compose_paths.covering_mount(shown, mounts),
            "manager": compose_paths.owner(shown),
        })
    return {"name": own, "mounts": mounts, "compose": rows}


def findings(state):
    """The report as `(kind, params)` pairs — one per thing worth saying.

    Pure: the same facts give the same lines, and a connection turns
    them into its own words. Kinds:

      `self_name`        who we are, or that we could not tell
      `self_mounts`      how many mounts, and which cover compose paths
      `compose_ok`       this file is readable — nothing to do
      `compose_no_mount` not readable, nothing mounted over it
      `compose_wrong`    not readable although a mount covers it
    """
    out = [("self_name", {"name": state.get("name") or ""})]
    out.append(("self_mounts", {"count": len(state.get("mounts") or [])}))
    for row in state.get("compose") or []:
        p = {"container": row["container"], "path": row["path"] or "?"}
        if not row["path"]:
            out.append(("compose_nolabel", {"container": row["container"]}))
        elif row["readable"]:
            out.append(("compose_ok", p))
        elif row["covered_by"]:
            src, dest = row["covered_by"]
            out.append(("compose_wrong", dict(p, src=src, dest=dest)))
        else:
            p["manager"] = row.get("manager") or ""
            p["mount"] = compose_paths.mount_root(row["path"] or "") or ""
            out.append(("compose_no_mount", p))
    return out


def summary(state):
    """`(total, readable, wrong, missing)` for a one-line headline."""
    rows = state.get("compose") or []
    ok = sum(1 for r in rows if r["readable"])
    wrong = sum(1 for r in rows if not r["readable"] and r["covered_by"])
    # A container whose label holds no path at all is neither: it is
    # counted with the rest so the four numbers still add up to `total`.
    return len(rows), ok, wrong, len(rows) - ok - wrong
