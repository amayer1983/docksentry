"""The rebuilt interface: data first, controls on demand.

Started from the question "what is this page for?" rather than from the
existing markup, after the owner said it had become hard to read. The
measurements agreed: 25 containers produced 236 forms, 380 fields and
289 buttons, and 426 of the page's explanations sat in `title` tooltips
that a phone cannot show at all.

Looking at it settled the design. Every row carried **six identical
emoji buttons** — magnifier, pin, recycle, red circle, arrows, question
mark — at the same size, in the same weight, with no way to tell the
routine one from the destructive one. And in a tool whose entire job is
telling you when a container needs updating, **the row did not say
whether it needed updating**. That fact lived in a counter at the top of
the page.

So, three decisions:

**The row leads with the answer.** State, name, image, and then either
"update available" with the button that applies it, or nothing at all.
An up-to-date container is quiet.

**Everything else moves into a detail panel.** Pin, auto-update,
restart, stop, logs, cooldown, notes: still one click away, but not
competing with the one thing you came to see. Six buttons per row become
one, and the ~200 controls that were on the page at all times become the
handful belonging to the container you actually opened.

**The page is data, not markup.** The server sends JSON and the browser
renders it, which is what makes the shell a few kilobytes instead of
173, lets a refresh update numbers without a reload, and means the row
layout can differ between a phone and a desktop without the server
knowing which one asked.

No framework and no build step — this project ships a small image with
no npm anywhere near it, and that is worth keeping. Rendering a list is
not a problem that needs 40 kB of library.
"""

import json


def _tag_of(image):
    """`postgres:17` → `17`, `nginx` → `latest`. Digests are not tags."""
    if not image:
        return ""
    last = image.rsplit("/", 1)[-1]
    if "@" in last:
        return ""
    return last.split(":", 1)[1] if ":" in last else "latest"


def _repo_of(image):
    last = (image or "").rsplit("/", 1)[-1]
    return last.split("@", 1)[0].split(":", 1)[0]


def container_rows(views, host_key):
    """Every container on every host, flattened, as plain dicts.

    Built from the same `_status_view` dicts the old table renders from,
    so the two cannot disagree about what is pinned, pending or on
    auto-update — there is one reader of the store, not two.
    """
    rows = []
    for view in views:
        host = view["host"]
        pending = set(view["pending_names"])
        pinned = set(view["pinned"])
        auto = set(view["auto_list"])
        groups = view["groups"] or {}
        notes = view["notes"] or {}
        links = view["links"] or {}
        advisories = view.get("advisories") or {}
        own = view.get("own_name") or ""
        for c in view["containers"]:
            name = c["name"]
            key = host_key(host, name)
            group = groups.get(name)
            rows.append({
                "key": key,
                "name": name,
                "host": host,
                "image": c.get("image", ""),
                "repo": _repo_of(c.get("image", "")),
                "tag": _tag_of(c.get("image", "")),
                "version": (c.get("labels") or {}).get(
                    "org.opencontainers.image.version", ""),
                "health": c.get("health", "") or "",
                "state": c.get("state", "") or "running",
                # The one thing the old row never said.
                "update": name in pending or key in pending,
                "pinned": name in pinned or key in pinned,
                "auto": name in auto or key in auto,
                "self": bool(own and name == own),
                "group": group[1] if group else "",
                "note": notes.get(name, ""),
                "link": links.get(name, ""),
                "advisory": advisories.get(name, ""),
            })
    rows.sort(key=lambda r: (not r["update"], r["host"], r["name"].lower()))
    return rows


#: What this caller is allowed to do. Today it has two values, because
#: today Docksentry has two kinds of caller: a logged-in human, and a
#: read-only API token. Roles and users are a plausible next step, and
#: the point of sending capabilities rather than letting the client
#: assume them is that the day they arrive, the server sends a shorter
#: list and the interface needs no change at all.
#:
#: To be very clear about what this is not: hiding a button is not a
#: permission. Every endpoint enforces its own access, and must keep
#: doing so — this only stops the interface offering something the
#: caller would be refused. A client that ignores it gets 403s, which is
#: the correct outcome.
ALL_CAPS = ("update", "lifecycle", "settings")


def capabilities(read_only=False):
    return {c: not read_only for c in ALL_CAPS}


def payload(views, host_key, stats=None, can=None):
    """The whole status page as one JSON document."""
    rows = container_rows(views, host_key)
    hosts = []
    for v in views:
        hosts.append({"name": v["host"],
                      "containers": len(v["containers"])})
    return {
        "containers": rows,
        "hosts": hosts,
        "stats": dict(stats or {}, containers=len(rows),
                      updates=sum(1 for r in rows if r["update"])),
        "can": can if can is not None else capabilities(),
    }


def shell(t, version, lang="en"):
    """The V2 page: a skeleton the browser fills in.

    Everything visible is rendered client-side from `/api/v2/status`, so
    what the server sends is a few kilobytes regardless of how many
    containers there are. The old page grew to 173 kB at 25 of them.
    """
    return f"""
<div id="v2" class="v2" data-lang="{lang}">
  <header class="v2-bar">
    <div class="v2-stats" id="v2-stats"></div>
    <div class="v2-tools">
      <input type="search" id="v2-search" class="v2-search"
             placeholder="{t('web_search_placeholder')}" autocomplete="off">
      <select id="v2-host" class="v2-host" hidden></select>
      <button type="button" class="v2-btn v2-btn-primary" id="v2-check">
        {t('web_check_updates')}
      </button>
    </div>
  </header>

  <div class="v2-filters" id="v2-filters">
    <button type="button" class="v2-chip is-on" data-filter="all"></button>
    <button type="button" class="v2-chip" data-filter="update"></button>
    <button type="button" class="v2-chip" data-filter="pinned"></button>
    <button type="button" class="v2-chip" data-filter="auto"></button>
  </div>

  <div id="v2-list" class="v2-list" aria-live="polite"></div>
  <p id="v2-empty" class="v2-empty" hidden></p>

  <!-- Selection puts the bulk bar on screen. It used to sit there
       greyed out at all times, taking a band of the page to say
       "nothing selected". -->
  <div id="v2-bulk" class="v2-bulk" hidden></div>

  <!-- Everything that used to be a sixth button in every row. -->
  <div id="v2-panel" class="v2-panel" hidden>
    <div class="v2-panel-sheet" role="dialog" aria-modal="true"></div>
  </div>
</div>
<script>window.DS_V2 = {json.dumps({"version": version, "lang": lang})};</script>
<script src="/static/v2.js?v={version}"></script>
"""
