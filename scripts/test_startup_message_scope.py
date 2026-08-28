#!/usr/bin/env python3
"""The startup banner is only sent where it exists.

`startup_msg` is built inside `if not post_selfupdate_restart:` — after
a self-update we deliberately stay quiet, because the update flow has
already said what happened and two near-identical restart messages
back-to-back is worse than one.

A stray line in the self-in-autoupdate migration block sent it a third
time, outside that block. Two independent conditions had to coincide:
Docksentry's own name in the auto-update list (so the migration fires
and strips it) and a start right after a self-update (so the name was
never bound). Then it is a NameError, thrown after the threads are up
and before the bot listener starts — a start that dies on the one
upgrade path that triggers the migration in the first place.

Short of that, it sent the same banner twice to every non-Telegram
channel.

This is a scope property, so it is checked as one: every read of the
name has to sit inside the block that binds it. A test that only
exercised the common path would have gone on passing.
"""
import ast
import os
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
src = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
tree = ast.parse(src)

checks = {}

assigns = [n.lineno for n in ast.walk(tree)
           if isinstance(n, ast.Assign)
           for t in n.targets
           if isinstance(t, ast.Name) and t.id == "startup_msg"]
reads = [n.lineno for n in ast.walk(tree)
         if isinstance(n, ast.Name) and n.id == "startup_msg"
         and isinstance(n.ctx, ast.Load)]

checks["the banner is built in exactly one place"] = len(assigns) == 1
checks["…and it is read somewhere"] = len(reads) >= 1


def guarding_block(tree, line):
    """The `if not post_selfupdate_restart:` body that contains `line`."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
                and isinstance(test.operand, ast.Name)
                and test.operand.id == "post_selfupdate_restart"):
            body = node.body
            if body and body[0].lineno <= line <= max(
                    getattr(x, "end_lineno", x.lineno) for x in body):
                return node
    return None


block = guarding_block(tree, assigns[0])
checks["it is built behind the post-selfupdate guard"] = block is not None

if block is not None:
    lo = block.body[0].lineno
    hi = max(getattr(x, "end_lineno", x.lineno) for x in block.body)
    outside = [ln for ln in reads if not lo <= ln <= hi]
    checks["every read sits inside that same guard"] = outside == []
    if outside:
        print(f"     read outside the guard at line(s): {outside}")

for k, v in checks.items():
    print(("  ✅" if v else "  ❌"), k)
if not all(checks.values()):
    print("FAIL")
    sys.exit(1)
print("PASS")
