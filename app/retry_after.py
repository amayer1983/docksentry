"""How long a `Retry-After` may actually be slept for.

Three call sites wait out a rate limit in line rather than queueing it —
`notifiers/base.py` for Discord webhooks and the generic webhook,
`telegram_bot.py`, `discord_rest.py` — and that is the right thing for a
limit that clears in two seconds: a trip through the retry queue costs
the message thirty seconds for nothing.

All three had the same cap written as the same literal, and an audit on
11.10.2026 took that apart three ways:

* **The cap was per attempt, not per call.** `Retry-After: 60` twice
  means two sleeps, and the retry loop runs three times. Measured:
  `slept=[60.0, 60.0]` — two minutes on the scheduler thread, which is
  the thread that also runs the monitor ticks and the queue flush. The
  commit that introduced the cap claimed "a rate limit that cannot put
  the scheduler to sleep"; two minutes is sleep.
* **A negative value went straight through.** `float("-1") <= 60` is
  true and `time.sleep(-1)` raises `ValueError`. Measured: it propagates
  out of `post_json_with_retry`, past a channel's promise never to raise
  into the facade, and the alert is lost *without* reaching the queue,
  because `on_network_failure` is never called.
* **"The same cap" was a coincidence of literals.** Nothing held the
  three to each other. Now there is one number and one rule, and the
  three sites ask this.

A server is allowed to answer anything at all here, including a date,
`inf`, or a hostile number. Everything this cannot use becomes "do not
sleep", which hands the message to the retry queue — the slower but
always-correct path.
"""

#: The most in-line waiting one send may do, in total across its
#: attempts. Beyond this the message goes to the retry queue instead,
#: which comes back every thirty seconds for a quarter of an hour and
#: does not hold a thread while it waits.
MAX_TOTAL_WAIT = 60.0


def usable_wait(wait, already_slept=0.0, budget=MAX_TOTAL_WAIT):
    """Seconds to sleep for this `Retry-After`, or None for "queue it".

    `already_slept` is what this *call* has waited so far, so the budget
    is spent once rather than once per attempt.
    """
    if wait is None:
        return None
    try:
        wait = float(wait)
    except (TypeError, ValueError):
        return None
    # `nan` fails every comparison, so it would slip through a `>` test
    # and reach `time.sleep` — which raises on it. `inf` is caught by
    # the budget below, but saying so here keeps the three rules in one
    # place rather than relying on arithmetic further down.
    if wait != wait or wait in (float("inf"), float("-inf")):
        return None
    if wait < 0:
        return None
    try:
        remaining = budget - float(already_slept or 0.0)
    except (TypeError, ValueError):
        remaining = budget
    if remaining <= 0 or wait > remaining:
        return None
    return wait
