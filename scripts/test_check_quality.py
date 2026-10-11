#!/usr/bin/env python3
"""The tests are code too, and they rot in four specific ways.

An outside review of the whole suite on 23.09.2026 found seventeen
checks that were wrong rather than merely weak — a different count from
the six vacuous ones the pre-release audit of that day's diff turned up,
and a different set: this one looked at all 178 files, that one at what
was about to be tagged. Among the seventeen: a claim asserting its own
opposite, `(A and B) or C` that passed on C alone so half of it was
never tested, a bare `True`, an `all()` over a list the line above
asserts is empty, and — the one that cost real coverage — a check key
written as an f-string with nothing to interpolate, inside a loop, so
three of four language checks silently overwrote each other.

None of that is carelessness. A grep is quick to write and a behavioural
harness is not, so the suite drifts that way unless something pushes
back. This is the push.

It is a ratchet, not a ban. Each pattern has a budget equal to what was
left after the clean-up, and the budget may only go down: adding one
fails, and removing one fails too, with a line telling you to lower the
number. That way the debt is visible, and it cannot quietly grow.

Deliberately NOT flagged: source-text assertions in general. Some things
can only be checked that way — an import ban, "this module never appears
in that one", a constant-time comparison. An AST sweep of the suite on
23.09.2026 counted 552 of them; a rule that cried about every one would
be switched off within a week.
"""
import ast
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

#: Pattern → how many are left. Lower these as you fix them; never raise.
BUDGET = {
    "bare-true": 0,
    "count-threshold": 16,
    "and-or-precedence": 0,
    "dead-fstring-key": 0,
}

WHY = {
    "bare-true": "`checks[...] = True` proves the line was reached, nothing more",
    "count-threshold": "a `>= N` floor over `.count()` passes while the guarded "
                       "thing is missing, if something else gained an occurrence",
    "and-or-precedence": "`A and B or C` binds as `(A and B) or C` and passes on C alone",
    "dead-fstring-key": "an f-string key with nothing to interpolate overwrites "
                        "itself on every loop pass, so only the last check runs",
}


def _is_check_target(node):
    return (isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "checks")


def _guarded_lines(tree):
    """Lines where a bare boolean still carries a fact.

    Inside a `try:` with an `except`, a bare `True` says "nothing raised
    on the way here" — thin, but true. And inside the `except` itself, a
    bare `False` IS the failure arm; flagging that would be flagging the
    idiom working as intended.
    """
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Try) and n.handlers:
            for stmt in list(n.body) + [b for h in n.handlers for b in h.body]:
                for sub in ast.walk(stmt):
                    if hasattr(sub, "lineno"):
                        out.add(sub.lineno)
    return out


def scan():
    """`{pattern: [file:line]}` over every test in this directory."""
    found = {k: [] for k in BUDGET}
    # Including itself. It used to skip its own file, and its own
    # pass-line was a bare `True` — a rule that exempts the rule is the
    # first thing an outside reader stops trusting.
    for path in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
        src = open(path, encoding="utf-8").read()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        name = os.path.basename(path)
        guarded = _guarded_lines(tree)
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Assign) and len(n.targets) == 1
                    and _is_check_target(n.targets[0])):
                continue
            where = f"{name}:{n.lineno}"
            key, val = n.targets[0].slice, n.value

            # `False` is never the problem: it cannot pass by accident,
            # and a lone one is somebody writing a failure arm by hand.
            if (isinstance(val, ast.Constant) and val.value is True
                    and n.lineno not in guarded):
                found["bare-true"].append(where)

            if (isinstance(key, ast.JoinedStr)
                    and not any(isinstance(v, ast.FormattedValue)
                                for v in key.values)):
                found["dead-fstring-key"].append(where)

            if isinstance(val, ast.BoolOp) and isinstance(val.op, ast.Or):
                # Ask whether the AND arm itself is parenthesised, not
                # whether the whole expression is: wrapping a long line
                # in brackets is normal here and used to hide the trap.
                for _v in val.values[:-1]:
                    if not (isinstance(_v, ast.BoolOp)
                            and isinstance(_v.op, ast.And)):
                        continue
                    _seg = ast.get_source_segment(src, _v) or ""
                    _at = src.find(_seg) if _seg else -1
                    if _at > 0 and src[_at - 1] == "(":
                        continue          # explicitly grouped: fine
                    found["and-or-precedence"].append(where)
                    break

            for c in ast.walk(val):
                if (isinstance(c, ast.Compare) and c.ops
                        and isinstance(c.ops[0], (ast.GtE, ast.Gt))
                        and isinstance(c.left, ast.Call)
                        and isinstance(c.left.func, ast.Attribute)
                        and c.left.func.attr == "count"):
                    found["count-threshold"].append(where)
                    break
    return found


def main():
    found = scan()
    checks = {}
    for pattern, budget in sorted(BUDGET.items()):
        hits = found[pattern]
        n = len(hits)
        if n > budget:
            checks[f"no new '{pattern}' — {WHY[pattern]}"] = False
            for h in hits[budget:][:8]:
                print(f"      → {h}")
        elif n < budget:
            checks[f"'{pattern}' budget is {budget} but only {n} left "
                   f"— lower it in BUDGET"] = False
        else:
            checks[f"'{pattern}' stays at {n}"] = n == budget

    # The scanner has to be able to see something, or a refactor that
    # renamed `checks` would turn this whole file into a green no-op.
    total = sum(len(v) for v in found.values())
    checks["the scanner is looking at real files"] = (
        len(glob.glob(os.path.join(HERE, "test_*.py"))) > 100 and total > 0)

    bad = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    print("FAIL" if bad else "PASS")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
