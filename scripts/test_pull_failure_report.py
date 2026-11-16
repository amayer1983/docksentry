#!/usr/bin/env python3
"""A failed pull has to say why, and the pull has to be given enough time.

Both halves come from the same report (#2, @famewolf, 05.11.2026). He
tried to update a 19.2 GB image and got:

    Compose pull failed:  Image ghcr.io/selfref/llama-swap-rdna:full
    Pulling 7a48b8f0e5c7 Pulling fs layer 0B 85b79304d1ba Pulling fs
    layer 0B 62548cdd3db4 Pulling fs layer 0B 9991dd98f842 Pulling fs
    layer 0B 4f4fb700ef

— and it stops there, mid layer ID, at exactly 200 characters. Compose
writes its progress to stderr, so the first 200 characters are layer
noise and the one line that says what went wrong sits at the END, which
is the part we threw away. He could not tell a timeout from a refused
manifest, and neither could I.

He also said he had "extended the global time limit to 180". There is no
such setting, and neither of the two he could have meant goes anywhere
near a pull: both apply to the `up` step after it. The pull's own limit
was 1800 seconds, hardcoded from v1.7.0 to 2.18.0, in no document and on
no page. For a 19 GB image that is about 11 MB/s sustained — a limit that
can expire on a line that is working fine, which nobody could raise.

So, two promises, driven rather than searched for:

* a pull failure reports the END of what the tool said, and
* the time a pull may take comes from the configuration.

The counter-directions matter as much as the promises here. A check that
only asserts "the reason is in the message" would also pass on a message
that is simply never truncated, so this asserts the opposite too: that
the old head-only cut really would have lost this reason. And a check
that only asserts "the timeout is a number" would pass on the hardcoded
1800, so two different configured values are driven and each has to come
out the other end.
"""

import inspect
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import config as config_mod          # noqa: E402
from errfmt import clip              # noqa: E402
from update_checker import UpdateChecker  # noqa: E402

checks = {}

# ── the stderr compose actually produces ─────────────────────────────
# Progress first, diagnosis last. The tail is the only part worth having.
REASON = "manifest for ghcr.io/selfref/llama-swap-rdna:full not found"
NOISE = " ".join(
    f"{i:012x} Pulling fs layer 0B" for i in range(1, 22))
RAW = f" Image ghcr.io/selfref/llama-swap-rdna:full Pulling {NOISE}\n" \
      f"Error response from daemon: {REASON}\n"

# This is what makes the whole file non-vacuous: on the old code the
# reason was NOT in the first 200 characters, so if the assertions below
# pass while this one fails, the test is measuring nothing.
checks["the reason is out of reach of a head-only cut"] = (
    REASON not in RAW[:200] and len(RAW) > 200)


class FakeBackend:
    """Stands in for the Docker CLI — the environment, not the decision.

    It records what it was asked to run and answers with a canned
    failure. Nothing here decides what the message says or how long the
    pull may take; that stays in update_checker.
    """

    name = "local"

    def __init__(self):
        self.calls = []

    def run(self, args, timeout=None, **kw):
        self.calls.append(("run", list(args), timeout))
        return types.SimpleNamespace(returncode=1, stdout="", stderr=RAW)

    def pull(self, ref, timeout=None, **kw):
        self.calls.append(("pull", [ref], timeout))
        return types.SimpleNamespace(returncode=1, stdout="", stderr=RAW)

    def inspect(self, *a, **kw):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")


def _checker(pull_timeout):
    cfg = types.SimpleNamespace(debug=False, pull_timeout=pull_timeout,
                                docker_stop_timeout=60)
    # The backend is a constructor seam, so the stand-in goes in there
    # rather than onto the instance: `backend` is a read-only property.
    chk = UpdateChecker(cfg, backend=FakeBackend())
    # Image metadata and history are environment, stubbed so the pull is
    # the only thing that happens.
    chk._get_image_created = lambda *a, **kw: "?"
    chk._get_image_size = lambda *a, **kw: "?"
    chk._get_image_version_label = lambda *a, **kw: None
    chk._save_history = lambda *a, **kw: None
    chk._layer_farewell = lambda *a, **kw: ""
    return chk


# ── 1. the compose path, end to end into the message ─────────────────
work = tempfile.mkdtemp()
compose_file = os.path.join(work, "docker-compose.yml")
with open(compose_file, "w") as f:
    f.write("services:\n  app:\n    image: ghcr.io/selfref/llama-swap-rdna:full\n")

chk = _checker(240)
ok, msg = chk._update_compose("rdna3-coder",
                              "ghcr.io/selfref/llama-swap-rdna:full",
                              "stack", "app", compose_file, work)

checks["a failed compose pull reports failure"] = ok is False
checks["…and the message names the step"] = "Compose pull failed" in msg
checks["…and carries the reason from the END of stderr"] = REASON in msg
checks["…and marks that it was trimmed"] = "[…]" in msg
checks["…and is one line, not compose's block"] = "\n" not in msg

# The pull really was the call that was made, with the configured limit.
pulls = [c for c in chk.backend.calls if "pull" in c[1]]
checks["the compose pull was actually run"] = len(pulls) == 1
checks["…with the timeout from the configuration (240)"] = (
    bool(pulls) and pulls[0][2] == 240)

# ── 2. a second value, so a hardcoded one cannot pass ────────────────
chk2 = _checker(7200)
chk2._update_compose("rdna3-coder", "ghcr.io/selfref/llama-swap-rdna:full",
                     "stack", "app", compose_file, work)
pulls2 = [c for c in chk2.backend.calls if "pull" in c[1]]
checks["a different setting gives a different timeout (7200)"] = (
    bool(pulls2) and pulls2[0][2] == 7200)
checks["…so the old hardcoded 1800 is gone"] = (
    bool(pulls2) and pulls2[0][2] != 1800)

# ── 3. the standalone path takes the same setting ────────────────────
chk3 = _checker(900)
ok3, msg3 = chk3._update_standalone("rdna3-coder",
                                    "ghcr.io/selfref/llama-swap-rdna:full")
standalone = [c for c in chk3.backend.calls if c[0] == "pull"]
checks["the standalone pull uses the setting too"] = (
    bool(standalone) and standalone[0][2] == 900)
checks["…and its message carries the reason as well"] = REASON in msg3

# ── 4. the setting is reachable, not just present ────────────────────
# Data, not source text: these are the registries that decide whether a
# value survives a save, can be set from the environment, and appears on
# a tab. A setting missing from any one of them is settable in theory
# only — which is how 1800 stayed invisible for two years.
checks["pull_timeout persists across a save"] = (
    "pull_timeout" in config_mod.PERSISTENT_KEYS)
# Named for what it actually asks. It reads the registry, which is
# what puts the field on the page — it is not evidence that anything
# reads the variable. Deleting the line in `from_env()` that reads
# PULL_TIMEOUT at all left this green while `PULL_TIMEOUT=7200`
# silently became 1800 (audit, 07.11.2026). That half is driven in
# test_env_bounds.py, for this variable and the ten beside it.
checks["…is registered as an environment variable"] = (
    config_mod.PERSISTENT_ENV_VARS.get("pull_timeout") == "PULL_TIMEOUT")
checks["…has the 1800 default recorded"] = (
    config_mod.PERSISTENT_ENV_DEFAULTS.get("pull_timeout") == 1800)
checks["…and sits on the Updates tab"] = (
    config_mod.PERSISTENT_SETTINGS_TAB.get("pull_timeout") == "Updates")

# The constructor must carry the same default the registry promises, or
# the environment and the object disagree about what "unset" means. Read
# off the signature itself — the first draft of this check asked the
# registry a second time, which is the registry agreeing with itself.
_sig = inspect.signature(config_mod.Config.__init__)
_param = _sig.parameters.get("pull_timeout")
checks["the constructor takes pull_timeout"] = _param is not None
checks["…defaulting to the same 1800 the registry promises"] = (
    _param is not None
    and _param.default == config_mod.PERSISTENT_ENV_DEFAULTS["pull_timeout"]
    and _param.default == 1800)
# …and it must not have become a required positional, which would break
# every existing caller of Config(...).
checks["…without becoming a required argument"] = (
    _param is not None and _param.default is not inspect.Parameter.empty)

# ── 4b. a rate limit speaks the user's language ──────────────────────
# From v1.7.0 to 2.18.0 this branch answered with two hardcoded German
# sentences — to all sixteen languages. The `rate_limit` key existed in
# every language file the whole time and had no caller at all, which is
# why no parity check ever noticed (audit, 16.11.2026).
import json as _json  # noqa: E402
from i18n import get_translator  # noqa: E402


class _RateLimited(FakeBackend):
    """Docker Hub's answer when you have pulled too much."""

    def pull(self, ref, timeout=None, **kw):
        self.calls.append(("pull", [ref], timeout))
        return types.SimpleNamespace(
            returncode=1, stdout="",
            stderr="toomanyrequests: You have reached your pull rate limit.")


def _rate_limit_message(lang):
    cfg = types.SimpleNamespace(debug=False, pull_timeout=1800,
                                docker_stop_timeout=60, language=lang)
    chk = UpdateChecker(cfg, backend=_RateLimited())
    chk._get_image_created = lambda *a, **kw: "?"
    chk._get_image_size = lambda *a, **kw: "?"
    chk._get_image_version_label = lambda *a, **kw: None
    chk._save_history = lambda *a, **kw: None
    chk._layer_farewell = lambda *a, **kw: ""
    _ok, msg = chk._update_standalone("db", "postgres:16")
    return msg


# Driven in three languages, each compared against that language's own
# file — not against a literal here, which would just be a second copy
# of the translation.
for _lang in ("en", "de", "ja"):
    _want = get_translator(_lang)("rate_limit")
    checks[f"a rate limit answers in {_lang}"] = (
        _rate_limit_message(_lang) == _want)

# The counter-direction, and the actual defect: the German sentence must
# not reach a reader who did not ask for German. A `return` of the plain
# key, or of English for everyone, also fails the three above — this one
# names the failure that really happened.
_ja = _rate_limit_message("ja")
checks["…and German does not leak into another language"] = (
    "erreicht" not in _ja and "ausführen" not in _ja)
checks["…and the key itself is not what comes out"] = _ja != "rate_limit"

# ── 5. clip's own promise, both ends ─────────────────────────────────
# Driven here because this is the helper the fix leans on, and it was
# written for this same thread in September and then not used on this
# path. Head kept, tail kept, middle gone.
long_err = "A" * 400 + "the reason"
clipped = clip(long_err)
checks["clip keeps the tail"] = clipped.endswith("the reason")
checks["…and the head"] = clipped.startswith("A")
checks["…and stays within its budget"] = len(clipped) <= 400

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
