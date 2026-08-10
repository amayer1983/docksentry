#!/usr/bin/env python3
"""A `tcp://` host is unencrypted, and now we say so (#60, @NotRetarded).

He accepted the argument for leaving TLS to a reverse proxy, and then
added: "I was thinking along the lines of encryption when communicating
using multi-hosts but the reverse proxy solves that issue."

It does not, and that is our fault rather than his. A reverse proxy sits
in front of the Web UI and protects people coming *in*. The multi-host
connection runs the other way, out from Docksentry to a remote Docker
daemon, and never passes through it. On a plain `tcp://` that link has no
TLS and no authentication at all: reaching the port is enough to start a
container that mounts the host filesystem, which is root on that machine.

Checked before writing any of this: the words "unencrypted", "cleartext"
and their neighbours appeared nowhere in the README or the docs, and
`docs/security.md` did not mention multi-host once — while the README's
own multi-host example put `tcp://pve1:2375` next to `ssh://root@nas`
with nothing to distinguish them. Somebody copying that line had one host
in the clear and no way to know.

So: a startup warning naming the host, a section in the security docs, and
an example that no longer teaches the wrong thing.
"""

import os
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from hosts import _is_plaintext_remote as plaintext  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
checks = {}

# ── which endpoints are the dangerous ones ───────────────────────────
for endpoint in ("tcp://pve1:2375", "tcp://192.168.1.5:2375",
                 "tcp://nas.local:2375", "tcp://10.0.0.9:2376"):
    checks[f"warns about {endpoint}"] = plaintext(endpoint)

# Encrypted, or never leaving the machine. A wrong warning is worse than
# none: it teaches people to ignore the next one.
for endpoint in ("ssh://root@nas", "ssh://user@host/run/podman/podman.sock",
                 "unix:///var/run/docker.sock", "context://nas", "", None):
    checks[f"stays quiet about {endpoint or '(empty)'}"] = not plaintext(endpoint)

# Loopback does not cross a network, so there is nothing to intercept.
for endpoint in ("tcp://localhost:2375", "tcp://127.0.0.1:2375",
                 "tcp://[::1]:2375"):
    checks[f"stays quiet about {endpoint}"] = not plaintext(endpoint)

# And the honest limitation: a socket proxy reached by its container name
# is indistinguishable from a LAN hostname, so it warns. Asserted rather
# than hidden, because the comment in hosts.py claims exactly this and a
# comment that disagrees with its code is worse than no comment.
checks["a socket-proxy container name warns too (cannot be told apart)"] = (
    plaintext("tcp://socket-proxy:2375"))
src = open(os.path.join(ROOT, "app", "hosts.py"), encoding="utf-8").read()
checks["…and the message offers that reading"] = (
    "socket proxy on a private" in src)
# Comment markers stripped as well as whitespace: the sentence wraps
# across `#:` lines, so a plain join leaves "LAN #: hostname" behind.
_flat = " ".join(l.lstrip("# :") for l in src.splitlines())
_flat = " ".join(_flat.split())
checks["…and the comment says so rather than claiming otherwise"] = (
    "nothing here can tell `socket-proxy` from a LAN hostname" in _flat)

# ── the warning itself ───────────────────────────────────────────────
i = src.index("if plaintext:")
warn = src[i:i + 1400]
checks["the warning names the host"] = "{name!r}" in warn
checks["…and the endpoint"] = "{endpoint}" in warn
checks["…and says what the risk actually is"] = "root on it" in warn
checks["…and what to do instead"] = "ssh://" in warn and "security.md" in warn
# Once, at startup, not per check: an alert that repeats every cron run is
# an alert people filter out.
checks["it is emitted where the hosts are built, not per check"] = (
    src.count("WARNING: host") == 1)

# ── the documentation gap that caused the misunderstanding ───────────
sec = open(os.path.join(ROOT, "docs", "security.md"), encoding="utf-8").read()
checks["the security doc now covers multi-host"] = (
    "## Multi-host" in sec)
checks["…and says the reverse proxy does not cover it"] = (
    "reverse proxy" in sec and "never passes through it" in sec)
checks["…and compares the transports"] = all(
    s in sec for s in ("ssh://user@host", "tcp://host:2375", "context://name"))
checks["…and names the two cases where tcp:// is fine"] = (
    "socket proxy" in sec and "TLS client certificates" in sec)

readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
# The line people copy. It used to hand out a plaintext host silently.
examples = re.findall(r"DOCKER_HOSTS=([^\n]*)", readme)
checks["no README example configures a plaintext remote host"] = not any(
    plaintext(e.strip()) for ex in examples for e in ex.split(",")
    if ":" in e for e in [e.split(":", 1)[1]])
checks["…and the example points at the security doc"] = any(
    "security.md" in readme[max(0, readme.index(ex) - 300):readme.index(ex)]
    for ex in examples)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
