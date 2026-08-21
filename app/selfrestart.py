"""Going down on purpose — front-end-neutral (#63).

Stopping ourselves is easy; coming back is the container's job, and that
is a promise only its restart policy can make. Without one, a restart
button is a stop button with a friendlier label — and the person who
pressed it has just lost the bot they would have used to bring it back.
So the policy is checked first, and a container that would stay down is
told rather than stopped. If the check itself cannot be answered, that
counts as "no": going down with no way back is the worse of the two
mistakes.

This lived on the Telegram bot, and the Discord bot reached across into
that instance to borrow it (`bot._restart_policy`, `bot.restart_self`) —
where it silently did not work at all. `_restart_policy(checker=None)`
fell back to `self.checker`, and the Telegram bot has no such attribute,
so every Discord `/restart` with no container named answered "I cannot
tell which container I am" and refused — on every setup, since the
command existed. Which is why `checker` is a required argument here: the
fallback was not a convenience, it was the hiding place.

Fourth step of the core extraction agreed on 2026-08-20. The three pieces
are separate on purpose — the caller has to be able to get its answer out
BEFORE the process goes away, and only the caller knows when that has
happened.
"""

import os
import signal
import threading
import time


def policy(backend, checker):
    """(policy name, explanation). An empty name means "do not stop".

    `checker` answers which container we are; `backend` asks the daemon
    what that container's restart policy is. Both are required — see the
    module docstring for what the optional one cost.
    """
    own = ""
    try:
        own = (checker._own_container_name() if checker else "") or ""
    except Exception:
        own = ""
    if not own:
        return "", "I cannot tell which container I am."
    try:
        r = backend.run(
            ["inspect", "--format", "{{.HostConfig.RestartPolicy.Name}}",
             own], timeout=15)
    except Exception as e:
        return "", f"the daemon would not answer: {str(e)[:80]}"
    name = (getattr(r, "stdout", "") or "").strip()
    if getattr(r, "returncode", 1) != 0 or not name:
        return "", "the daemon did not report a restart policy."
    if name in ("no", "none", "<no value>"):
        return "", f"this container's restart policy is `{name}`."
    return name, ""


def record_request(config, by=""):
    """Leave a marker so the next boot knows this stop was asked for.

    Without it the banner reports an external stop signal and adds
    "Docksentry did not restart itself", which is exactly backwards. Best
    effort: a marker that cannot be written is a wrong banner, not a
    reason to stay up.
    """
    try:
        from container_store import atomic_write_json
        atomic_write_json(config.restart_request_file,
                          {"ts": time.time(), "by": by})
        return True
    except Exception as e:
        print(f"Could not record the restart request (non-fatal): {e}")
        return False


def go_down(delay=1.5):
    """Our own SIGTERM, after a beat.

    Deliberately not `docker restart` on ourselves — that asks the daemon
    to stop the very process making the request, and the answer would die
    mid-sentence. This way the shutdown handler runs its normal course.

    The beat is for the answer that is already on its way. A caller whose
    answer has NOT gone out yet must not rely on it being long enough:
    call this after delivering, not before.
    """
    threading.Timer(
        delay, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
