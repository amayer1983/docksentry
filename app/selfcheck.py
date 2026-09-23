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
import json
import os

import compose_paths

#: "I could not tell" — distinct from `None`, which is the measured
#: answer "nothing on this machine holds it".
UNKNOWN = object()


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


def _all_mounts(checker):
    """`[{name, image, type, vol, src, dest}]` for every container here.

    Every container, not only the running ones: a stopped Dockge still
    holds the directory its label points into, and answering "nothing on
    this machine has that file" because it happened to be down would be
    the confidently wrong answer this module exists to avoid.

    Empty when the daemon will not say. The caller then reports the
    holder as unknown, which is true, rather than as absent.
    """
    rows = []
    try:
        ids = checker.backend.ps(all=True, quiet=True, timeout=15)
        refs = ids.stdout.split() if getattr(ids, "returncode", 1) == 0 else []
        if not refs:
            return rows
        r = checker.backend.inspect(refs, timeout=30)
        # The exit code alone is not the answer. `docker inspect` exits 1
        # when ANY id is unknown and still prints valid JSON for all the
        # others — and `ps -aq` and `inspect` are two calls, so anything
        # that goes away in between (a recreate, a `--rm` job, our own
        # update) made us throw the whole reply away and answer "I could
        # not tell" on a host whose daemon knew perfectly well. Measured
        # on Docker 29.5.3: 25 good ids plus one dead one exits 1 and
        # prints 25 containers.
        try:
            parsed = json.loads(r.stdout) or []
        except (ValueError, TypeError):
            return rows
        for ins in parsed:
            img = (ins.get("Config", {}).get("Image") or "").lower()
            for m in ins.get("Mounts") or []:
                dest = (m.get("Destination") or "").rstrip("/")
                if dest:
                    rows.append({"name": (ins.get("Name") or "").lstrip("/"),
                                 "image": img, "type": m.get("Type"),
                                 "vol": m.get("Name") or "",
                                 "src": m.get("Source") or "",
                                 "dest": dest})
    except Exception:                                   # noqa: BLE001
        return []
    return rows


def _held_by(path, rows, own, mine=()):
    """`(source, dest, who)`, `None` for "nothing here holds it", or
    `UNKNOWN` for "I could not tell".

    Those last two are different answers and were the same one until the
    pre-release audit caught it: an ambiguous lookup, and a daemon that
    would not answer at all, both came out as the confident line
    "nothing on this machine holds those paths". This module's own rule
    is the opposite — a fact we could not establish is reported as
    unknown rather than guessed — and it was breaking it.

    Our own mounts are struck out first. The only paths this is asked
    about are ones we could NOT read, so a mount of ours over that path
    has already proved it does not hold the file — naming it back at the
    reader would hand them the very directory they are trying to
    replace (#63: three mounts, none of them the one that was meant).
    """
    if not rows:
        return UNKNOWN                      # the daemon told us nothing
    # By name where we have one, and by the mounts themselves where we do
    # not: `_own_container_name()` comes back empty often enough (no
    # HOSTNAME, an inspect that fails) and the name test alone then
    # excluded nothing, so our own wrong mount could be handed back as
    # the answer — the #63 loop, from the other end.
    ours = {(src or "", (dest or "").rstrip("/")) for src, dest in (mine or [])}
    others = [r for r in rows
              if r.get("name") != own
              and ((r.get("vol") or r.get("src") or ""),
                   (r.get("dest") or "").rstrip("/")) not in ours]
    found = compose_paths.holder(path, others)
    if found is compose_paths.AMBIGUOUS:
        return UNKNOWN
    if not found:
        return None
    src, dest, who = found
    if not who:
        # An unnamed container would render "inside ``" — the same hole
        # this release fixed for the mount path. Say what we have.
        who = next((r.get("image") or "" for r in others
                    if r.get("dest") == dest and
                    (r.get("vol") or r.get("src")) == src), "")
    return (src, dest, who)


def _shadows_our_data(dest, data_dir):
    """Would mounting something at `dest` hide our own state?

    True when `dest` IS our data directory or sits above it. The web
    page has refused that suggestion since #2 — Portainer keeps its
    stacks in a volume at `/data` and the legacy default put ours there
    too, so "mount portainer_data at /data" would have read-only-mounted
    a stranger's volume over our database.
    """
    d = (dest or "").rstrip("/")
    data = (data_dir or "").rstrip("/")
    if not d or not data:
        return False
    return d == data or data.startswith(d + "/")


def collect(checker, names):
    """Gather the facts. Returns the dict `render` expects.

    `checker` is an UpdateChecker — it already knows our own container
    name and our own mounts, and can read a container's compose labels.
    `names` are the containers to look at; a caller with a shorter list
    gets a shorter answer.
    """
    own = ""
    mounts = []
    data_dir = ""
    try:
        own = checker._own_container_name() or ""
        mounts = checker._own_mounts()
        data_dir = getattr(getattr(checker, "config", None), "data_dir", "") or ""
    except Exception:                                   # noqa: BLE001
        pass

    all_rows = _all_mounts(checker)

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
        readable = bool(files) and all(os.path.isfile(f) for f in files)
        rows.append({
            "container": name,
            "path": shown,
            # Measured, not deduced: we ask the filesystem.
            "readable": readable,
            "covered_by": compose_paths.covering_mount(shown, mounts),
            "manager": compose_paths.owner(shown),
            # Who does have it. Asked for every file we could not read,
            # and used by both branches below — a mount that points at
            # the wrong place and no mount at all want the same answer.
            "held_by": None if readable else _held_by(shown, all_rows, own, mounts),
        })
    return {"name": own, "mounts": mounts, "compose": rows,
            "data_dir": data_dir}


def findings(state):
    """The report as `(kind, params)` pairs — one per thing worth saying.

    Pure: the same facts give the same lines, and a connection turns
    them into its own words. Kinds:

      `self_name`        who we are, or that we could not tell
      `self_mounts`      how many mounts, and which cover compose paths
      `compose_ok`       this file is readable — nothing to do
      `compose_no_mount` not readable, nothing mounted over it
      `compose_wrong`    not readable although a mount covers it
      `compose_holder`   which container does hold it, and where from
      `compose_clash`    it is here, but mounting it would hide our data
      `compose_elsewhere` nothing here holds it — it is on another machine
      `compose_holder_unknown` we could not tell, and will not guess
    """
    out = [("self_name", {"name": state.get("name") or ""})]
    out.append(("self_mounts", {"count": len(state.get("mounts") or [])}))
    elsewhere = False
    unknown = False
    for row in state.get("compose") or []:
        p = {"container": row["container"], "path": row["path"] or "?"}
        if not row["path"]:
            out.append(("compose_nolabel", {"container": row["container"]}))
        elif row["readable"]:
            out.append(("compose_ok", p))
        elif row["covered_by"]:
            src, dest = row["covered_by"]
            out.append(("compose_wrong", dict(p, src=src, dest=dest)))
            # And the half that ends the search: which directory would
            # have worked, or that nothing here holds the file at all.
            held = row.get("held_by")
            if held is UNKNOWN or not held:
                # Nothing to add. Whether we could not tell or nothing
                # here holds it is decided once, below — saying either
                # per container turns one problem into five lines.
                unknown = unknown or held is UNKNOWN
                elsewhere = elsewhere or held is None
            else:
                h_src, h_dest, h_who = held
                if _shadows_our_data(h_dest, state.get("data_dir")):
                    # The web page has refused this since #2 and the
                    # audit was handing it out: mounting over our own
                    # data directory hides our state. Portainer keeps
                    # its stacks in a volume at /data and so did we.
                    out.append(("compose_clash",
                                dict(p, src=h_src, dest=h_dest)))
                else:
                    out.append(("compose_holder",
                                dict(p, src=h_src, dest=h_dest, who=h_who)))
        elif row.get("held_by") and row["held_by"] is not UNKNOWN:
            # Nothing is mounted over it AND we know who has it. The
            # generic line would say "mount /data/compose", a host
            # directory that does not exist when the stacks live in
            # Portainer's volume — the complaint #2 was about.
            h_src, h_dest, h_who = row["held_by"]
            if _shadows_our_data(h_dest, state.get("data_dir")):
                out.append(("compose_clash", dict(p, src=h_src, dest=h_dest)))
            else:
                out.append(("compose_holder",
                            dict(p, src=h_src, dest=h_dest, who=h_who)))
        else:
            p["manager"] = row.get("manager") or ""
            # A manager we know gets its root, because one mount covers
            # every stack it holds. Everything else gets the file's own
            # directory — which is what the container page has always
            # offered, while this line printed "Mount  to reach it" with
            # a hole where the path belongs (#63, seen on 12 stacks at
            # once).
            p["mount"] = (compose_paths.mount_root(row["path"] or "")
                          or os.path.dirname(row["path"] or "")
                          or row["path"] or "?")
            out.append(("compose_no_mount", p))
    # Said once, not per container: the three that are missing are
    # usually the same manager, and three identical lines read as three
    # problems. "Elsewhere" is a claim, so it is only made when nothing
    # was found anywhere — beside a line naming a container right here,
    # it contradicted itself.
    named = any(k in ("compose_holder", "compose_clash") for k, _p in out)
    if unknown:
        out.append(("compose_holder_unknown", {}))
    elif elsewhere and not named:
        out.append(("compose_elsewhere", {}))
    # …and when some were found and others positively were not, neither
    # line is true of the whole set, so neither is said. The per-container
    # lines above have already carried it. Saying "I could not tell"
    # there was the first fix for the contradiction and it was worse: the
    # daemon HAD told us, for every one of them.
    return out


def summary(state):
    """`(total, readable, wrong, missing)` for a one-line headline."""
    rows = state.get("compose") or []
    ok = sum(1 for r in rows if r["readable"])
    wrong = sum(1 for r in rows if not r["readable"] and r["covered_by"])
    # A container whose label holds no path at all is neither: it is
    # counted with the rest so the four numbers still add up to `total`.
    return len(rows), ok, wrong, len(rows) - ok - wrong
