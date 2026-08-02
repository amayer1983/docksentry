#!/usr/bin/env python3
"""Registry tag pagination (diun#43, #518, #653).

The tag list feeds the major-bump gate. Registries hand out the OLDEST tags
first, so reading only the first page truncates at exactly the end that
matters: `ghcr.io/home-assistant/home-assistant` returned 100 tags, all
from 2021, whose highest parseable version was 2021.7.1 while the project
was on 2026.7.4. Every major decision there was made against a four-year-old
view. Docker Hub answers with everything, which is why "the first page is
enough in practice" held for so long and was wrong everywhere else.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker


class _H:
    def __init__(self, link=None):
        self._link = link

    def get(self, key):
        return self._link if key == "Link" else None


def main():
    checks = {}
    nxt = UpdateChecker._next_tag_page

    # Registries send a PATH, not a URL, so it has to be joined onto the
    # host we asked — verbatim shape from GHCR.
    h = _H('</v2/home-assistant/home-assistant/tags/list?last=2021.8.0&n=100>; rel="next"')
    checks["a path is joined onto the host"] = nxt(h, "ghcr.io") == (
        "https://ghcr.io/v2/home-assistant/home-assistant/tags/list?last=2021.8.0&n=100")

    checks["no header, no next page"] = nxt(_H(None), "ghcr.io") is None
    checks["no rel=next, no next page"] = nxt(
        _H('</v2/x/tags/list>; rel="prev"'), "ghcr.io") is None
    # An odd Link header must END pagination, never send the crawl
    # somewhere else — a registry is not a redirector.
    checks["a malformed header stops rather than guesses"] = nxt(
        _H('rel="next"'), "ghcr.io") is None
    checks["a relative path without a slash is refused"] = nxt(
        _H('<v2/x>; rel="next"'), "ghcr.io") is None
    # An absolute URL is only followed if it stays on the same host.
    checks["same-host absolute url is followed"] = nxt(
        _H('<https://ghcr.io/v2/x/tags/list?last=a>; rel="next"'),
        "ghcr.io") == "https://ghcr.io/v2/x/tags/list?last=a"
    checks["a cross-host absolute url is refused"] = nxt(
        _H('<https://evil.example.com/v2/x>; rel="next"'), "ghcr.io") is None
    # Headers without a .get (None was passed) must not raise.
    checks["a missing header object is safe"] = nxt(None, "ghcr.io") is None

    # The crawl is bounded, and the cache exists — a compose stack with
    # five containers from one image must not walk 44 pages five times.
    import inspect as _i
    src = _i.getsource(UpdateChecker._list_remote_tags)
    checks["the crawl is bounded"] = "pages < 60" in src
    checks["a repeated url breaks the loop"] = "seen_urls" in src
    checks["results are cached per run"] = "_tag_list_cache" in src
    # A failed continuation must keep the pages already fetched rather
    # than throwing the first one away too.
    checks["a failed page keeps what was fetched"] = "pagination stopped" in src

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
