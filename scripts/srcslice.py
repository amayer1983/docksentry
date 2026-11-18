#!/usr/bin/env python3
"""Cut a piece out of app/ source without dying when the anchor is gone.

A lot of procedures read a branch or a method out of the source text
with `src.index(anchor)`, `src.split(anchor)[1]` or `inspect.getsource`.
That is fine while the code stands still. The core split moves exactly
that code, and a moved anchor made those procedures raise: ValueError,
IndexError, AttributeError, a traceback and no verdict. `verify.py`
then shows a red procedure with nothing to say, and
`test_guard_mutations.py` scores every entry that names it as
`[cannot run]` — far away from the line that actually moved.

Everything here answers `None` instead of raising, and prints one
`FAIL` line that names what it was looking for. A caller has to treat
`None` as "this check is red": `x is not None and "y" in x`, never
`"y" in (x or "")`. An empty string would make every `not in` check
pass on a branch that no longer exists, which is worse than the crash.

Not a `test_*.py` file on purpose, so `verify.py` does not run it as a
procedure of its own.
"""

import inspect


def _fail(label, what):
    print(f"  FAIL  {label}: anchor not found")
    print(f"      → looked for {what!r}")


def position(src, needle, start=0, *, label):
    """Index of the first `needle` at or after `start`, or None."""
    i = -1 if src is None else src.find(needle, start)
    if i < 0:
        _fail(label, needle)
        return None
    return i


def section(src, start, end=None, *, label, length=None):
    """The source from the first `start` up to the next `end` after it.

    `start` is included, `end` is not. `end` is looked for behind the
    whole of `start`, so an end anchor that is a prefix of the start
    cannot cut the piece to nothing. Without `end` the piece runs to the
    end of `src`, or for `length` characters when that is given.

    None, with a FAIL line, when either anchor is missing.
    """
    i = position(src, start, label=label)
    if i is None:
        return None
    if end is None:
        return src[i:] if length is None else src[i:i + length]
    j = position(src, end, i + len(start), label=label)
    if j is None:
        return None
    return src[i:j]


def attr(owner, name, *, label=None):
    """`getattr(owner, name)`, or None with a FAIL line.

    For procedures that drive a method rather than read it: a method a
    later step moved somewhere else should read as a red check, not as
    an AttributeError.
    """
    try:
        return getattr(owner, name)
    except AttributeError:
        _fail(label or f"{_name(owner)}.{name}", name)
        return None


def method_source(owner, name, *, label=None):
    """`inspect.getsource(owner.name)`, or None with a FAIL line."""
    obj = attr(owner, name, label=label)
    if obj is None:
        return None
    try:
        return inspect.getsource(obj)
    except (OSError, TypeError):
        _fail(label or f"{_name(owner)}.{name}", f"source of {name}")
        return None


def in_order(text, *needles):
    """True when every needle is in `text`, each first one after the
    one before it. False rather than ValueError when one is missing."""
    if text is None:
        return False
    at = [text.find(n) for n in needles]
    return all(i >= 0 for i in at) and all(
        a < b for a, b in zip(at, at[1:]))


def _name(owner):
    return getattr(owner, "__name__", type(owner).__name__)
