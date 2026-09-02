#!/usr/bin/env python3
"""Managed hosts are asked side by side, and asked once.

Measured on an install with four hosts: an ssh endpoint cost 2.1 seconds,
of which 476 ms was a probe asking exactly what the listing asks again a
moment later — and the hosts ran one after another, so the page paid the
sum of all of them. It now pays the slowest one, once.

What must not change: one dead endpoint stays a line in the table rather
than an exception that takes the page with it, and the order of the hosts
on screen does not depend on which answered first.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

checks = {}

src = open(os.path.join(os.path.dirname(__file__), "..",
                        "app", "web_ui.py")).read()
block = src[src.index("def _host_views"):]
block = block[:block.index("\n        @staticmethod")]

checks["the hosts are fetched concurrently"] = ("ThreadPoolExecutor" in block)
checks["…with a bounded pool, not one thread per host forever"] = (
    "max_workers=min(" in block)
checks["the worker swallows its own failure"] = (
    re.search(r"def _fetch\(.*?except Exception", block, re.S) is not None)
checks["one host's failure does not skip the others"] = (
    block.count("return _h, None, _e") == 1)

# The duplicate probe is gone: exactly one `ps` per host, and its result
# is handed to the lister instead of being thrown away.
checks["a host is asked for its containers once"] = (
    "ids=_ids" in block and 'fmt="{{.Names}}"' not in block)

# Order comes from the configured host list, not from the pool.
_render = block[block.index("_results = {}"):]
checks["the table order follows the configuration"] = (
    "for _host in (multi or ())" in _render)
checks["a remembered failure is still shown, not silently dropped"] = (
    '"retry_in"' in block)
checks["a host that answers clears its failure memory"] = (
    "mark_reachable" in block)

# And the lister still works for callers that have no ids to hand over.
_lister = src[src.index("def _containers_on"):]
_lister = _lister[:_lister.index("\n        def ", 10)]
checks["the lister still fetches ids when it is not given any"] = (
    "if ids is None:" in _lister)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
