#!/usr/bin/env python3
"""Every channel announces the same thing, in your language (#63).

The bots were brought onto shared wording first; the notification
channels were not, and the check that found it was asking whether the fix
had actually reached everywhere. It had not. Six channels each wrote
their own English for the two structured notifications — six slightly
different sentences — so an instance set to German announced updates in
German on Telegram and in English on ntfy, e-mail, Gotify, Matrix,
Apprise and the Discord webhook.

Worse than the language: two of the six tagged the host and four did not.
`plex` on the NAS and `plex` at home are different containers, and which
one had an update depended on which channel you happened to read.

So the sentences live in `notify_text`, translated, and a channel decides
only its own presentation — bullet character, embed shape, HTML.

Two bugs this test was written after finding, both caught by rendering
all six side by side rather than by reading the diff:

  * Matrix dropped the intro line from its plain text while keeping it in
    its HTML — one event, two renderings, different words;
  * the Discord embed left the host tag out of its field names, which is
    exactly the drift being removed.
"""
import inspect
import json as _json
import os
import sys
import types

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)

import notify_text  # noqa: E402
import notifiers.ntfy as N  # noqa: E402
import notifiers.smtp as S  # noqa: E402
import notifiers.gotify as G  # noqa: E402
import notifiers.matrix as M  # noqa: E402
import notifiers.apprise as A  # noqa: E402
import notifiers.discord as D  # noqa: E402

checks = {}

UPDATES = [
    {"name": "plex", "image": "plex:latest", "host": "nas",
     "size": "1.2GB", "created": "2026-08-01"},
    {"name": "web", "image": "nginx:1", "size": "60MB",
     "created": "2026-07-01"},
]


def config(lang):
    return types.SimpleNamespace(
        language=lang, bot_label="", discord_webhook="http://x",
        ntfy_url="https://ntfy.sh/t", smtp_host="h", smtp_from="a",
        smtp_to="b", gotify_url="http://g", gotify_token="t",
        matrix_homeserver="http://m", matrix_token="t", matrix_room="!r",
        apprise_url="http://a", apprise_urls="", apprise_tag="")


def channel_class(mod):
    for name, cls in inspect.getmembers(mod, inspect.isclass):
        if cls.__module__ == mod.__name__ and name.endswith("Notifier"):
            return cls
    return None


def render(mod, lang, method, *args):
    """Everything one channel would put on the wire, as one string."""
    cls = channel_class(mod)
    if cls is None:
        return ""
    obj = cls.__new__(cls)
    obj.config = config(lang)
    obj.version_str = lambda u: ""
    obj._bot_label = lambda: ""
    obj._footer_text = lambda: ""
    obj._title = lambda t: t
    obj._prefix = lambda t: t
    out = []
    obj._post = lambda *a, **k: out.append(" ".join(str(x) for x in a))
    obj.send_raw = lambda *a, **k: out.append(" ".join(str(x) for x in a))
    obj._send = lambda *a, **k: out.append(" ".join(str(x) for x in a))
    obj.post = lambda p: out.append(_json.dumps(p, ensure_ascii=False))
    getattr(obj, method)(*args)
    return "\n".join(out)


CHANNELS = {"ntfy": N, "smtp": S, "gotify": G, "matrix": M,
            "apprise": A, "discord": D}

# ── the announcement, in German, on every channel ────────────────────
rendered = {name: render(mod, "de", "send_updates_available", UPDATES)
            for name, mod in CHANNELS.items()}
title_de = notify_text.updates_available(UPDATES, lang="de")[0]
for name, text in rendered.items():
    checks[f"{name} announces updates in the configured language"] = (
        title_de in text)
    # The host tag is content, not decoration: two containers share a name.
    checks[f"{name} names the host the container is on"] = "@nas" in text

# English stays English — the shared wording, not a German string leaking.
title_en = notify_text.updates_available(UPDATES, lang="en")[0]
for name, mod in CHANNELS.items():
    checks[f"{name} follows a switch back to English"] = (
        title_en in render(mod, "en", "send_updates_available", UPDATES))

# ── the result of one update ─────────────────────────────────────────
ok_de = notify_text.update_result("web", "nginx:1", True, lang="de")[0]
bad_de = notify_text.update_result("web", "nginx:1", False, lang="de")[0]
for name, mod in CHANNELS.items():
    good = render(mod, "de", "send_update_result", "web", "nginx:1", True,
                  "some detail")
    bad = render(mod, "de", "send_update_result", "web", "nginx:1", False,
                 "it broke")
    checks[f"{name} reports a successful update in one wording"] = (
        ok_de in good)
    checks[f"{name} reports a failure in one wording"] = bad_de in bad
    checks[f"{name} keeps success and failure distinguishable"] = (
        good != bad)

# ── Matrix's two renderings must agree ───────────────────────────────
mx = render(M, "de", "send_updates_available", UPDATES)
intro = notify_text.updates_available(UPDATES, lang="de")[1].split("\n\n")[0]
checks["Matrix says the same in its plain text as in its HTML"] = (
    mx.count(intro) >= 2)

# ── the channels compose nothing of their own any more ───────────────
import re  # noqa: E402
for name, mod in CHANNELS.items():
    src = open(mod.__file__, encoding="utf-8").read()
    for gone in ("Docker update(s) available", "Docksentry found updates",
                 "update(s) available", "Update Successful", "Update Failed"):
        checks[f"{name} no longer writes {gone!r}"] = gone not in src

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    if not v:
        print(f"  FAIL {k}")
print(f"  ({len(checks)} checks across {len(CHANNELS)} channels)")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
