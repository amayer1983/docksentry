#!/usr/bin/env python3
"""An image that changes its ENTRYPOINT must not brick the update.

2.17.10 put an init in front of the entrypoint. That was the first time
a Docksentry image had ever changed it, and the update path could not
survive it:

  * `Config.Entrypoint` echoes the image's ENTRYPOINT when nobody
    overrode anything, so it only means "the user chose this" relative
    to the image the container was BUILT FROM. It was compared against
    the NEW image instead — which makes every image-side change look
    like a user override and pins the OLD entrypoint onto the new image.
  * The self-update then sliced its argv at the image and threw away
    everything after it. The `--entrypoint` flag survived; the script it
    pointed at did not.

Together: a container running a bare `python3`, which reads stdin, gets
EOF, exits 0, and is restarted by the restart policy. Measured at 7
restarts in 8 seconds; @NotRetarded reported over a thousand and a dead
install.

Both halves are asserted here, because either one alone is enough.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker                   # noqa: E402

checks = {}

OLD_IMAGE = {"Entrypoint": ["python3", "/app/main.py"], "Cmd": None}
NEW_IMAGE = {"Entrypoint": ["/sbin/tini", "--", "python3", "/app/main.py"],
             "Cmd": None}


def build(container_entrypoint, inherited, image_defaults=NEW_IMAGE,
          cmd=None):
    cfg = {"Config": {"Entrypoint": container_entrypoint, "Cmd": cmd,
                      "Image": "img:old", "Env": [], "Labels": {}},
           "HostConfig": {}, "Image": "img:old", "Name": "/ds"}
    return UpdateChecker._build_run_args(
        cfg, "img:new", "ds", image_defaults, inherited=inherited)


# ── an image that changes ENTRYPOINT is adopted, not overridden ──────
argv = build(["python3", "/app/main.py"], inherited=OLD_IMAGE)
checks["a container matching its own image is not an override"] = (
    "--entrypoint" not in argv)
checks["…so the new image's entrypoint is what runs"] = (
    "/app/main.py" not in argv[argv.index("img:new"):])

# ── a real override is still preserved, arguments and all ────────────
argv = build(["/usr/bin/myrunner", "--flag", "x"], inherited=OLD_IMAGE)
checks["a genuine override is kept"] = (
    "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1]
    == "/usr/bin/myrunner")
tail = argv[argv.index("img:new") + 1:]
checks["…with the rest of its tokens after the image"] = (
    tail[:2] == ["--flag", "x"])

# The failure mode in one line: the flag survived, its arguments did not.
# A multi-token entrypoint must never come out as the binary alone.
checks["an entrypoint is never emitted without its arguments"] = not (
    "--entrypoint" in argv and tail == [])

# ── the old image being gone is not treated as an override ───────────
argv = build(["python3", "/app/main.py"], inherited=None)
checks["an unknown old image emits no entrypoint at all"] = (
    "--entrypoint" not in argv)

# ── and the self-update carries what comes after the image ───────────
# On this line the swap still lives on the bot; the core refactor moves
# it to its own module. What it has to do is the same either way.
from telegram_bot import TelegramBot                        # noqa: E402

script = TelegramBot._build_selfupdate_script(
    "ds", "--name ds", "img:new", cmd_parts="--flag x")
checks["the swap script keeps the tokens after the image"] = (
    "img:new --flag x" in script)
script_plain = TelegramBot._build_selfupdate_script(
    "ds", "--name ds", "img:new")
checks["…and adds nothing when there are none"] = (
    "img:new || " in script_plain and "img:new  " not in script_plain)

src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "telegram_bot.py"), encoding="utf-8").read()
checks["the swap does not throw away the tail any more"] = (
    "cmd_args = full[img_idx + 1:]" in src)

# ── and the shipped image still starts the way the code expects ──────
dockerfile = open(os.path.join(os.path.dirname(__file__), "..",
                               "Dockerfile"), encoding="utf-8").read()
checks["the image's entrypoint runs main.py directly"] = (
    'ENTRYPOINT ["python3", "/app/main.py"]' in dockerfile)
# Why it is not an init, so nobody re-adds one without reading first.
checks["…and why it is not an init is written down"] = (
    "tini" in dockerfile and "ENTRYPOINT" in dockerfile
    and "self-update" in dockerfile)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
