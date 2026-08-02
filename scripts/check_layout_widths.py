#!/usr/bin/env python3
"""Measure the Web UI's layout across desktop widths, 1024 to 4K.

Not a unit test — it drives a real headless browser against a running
instance, because layout bugs do not show up in the markup. Two of them got
past code review and a look at the page and were only caught by measuring:

  * the container table scrolled the WHOLE PAGE sideways on a phone (286px
    at 360px wide), header and all;
  * the 900px content cap left the table scrolling by 48px on a 2560px
    monitor with 1700px of screen sitting empty (#46, @LeeNX).

Both were invisible until something counted pixels. So this counts pixels.

Run it against a live instance:

    python3 scripts/check_layout_widths.py [--url http://localhost:9090]

Needs Docker and pulls a headless-chrome image on first use. Skips with a
clear message rather than failing when either is missing — a machine with
no Docker should not fail the suite over a browser it cannot start.

Narrow widths are checked too, but held to a weaker rule on purpose: below
about 700px a data table genuinely cannot fit, and the table is allowed to
scroll inside its own container. What is never allowed, at any width, is
the *page* scrolling sideways.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

CHROME_IMAGE = "zenika/alpine-chrome:latest"

#: Desktop widths where the table is expected to fit without scrolling.
#: 1024 is the smallest laptop worth supporting; 3840 is 4K.
DESKTOP = [1024, 1280, 1440, 1600, 1920, 2560, 3840]

#: Narrower widths. Here the table may scroll inside its container — but
#: the page still may not.
NARROW = [360, 390, 768]

PAGES = ["", "groups", "history", "logs", "settings"]

PROBE = """
<pre id="ds-probe"></pre>
<script>
var de = document.documentElement;
var vw = de.clientWidth;
var t = document.querySelector('#ctbl');
var box = t ? t.parentElement : null;
document.getElementById('ds-probe').textContent = JSON.stringify({
  viewport: vw,
  pageOverflow: Math.max(0, de.scrollWidth - vw),
  tableWants: t ? t.scrollWidth : 0,
  boxWidth: box ? box.clientWidth : 0,
  tableOverflow: (t && box) ? Math.max(0, t.scrollWidth - box.clientWidth) : 0
});
</script>
"""


def have_docker():
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "info"], capture_output=True)
    return r.returncode == 0


def build_page(url, page, css, out):
    """Save the page with its stylesheet inlined and a probe appended.

    Inlined rather than fetched, because the browser runs from `file://`
    with no network — which also keeps the measurement independent of
    whether the instance is reachable from inside a container.
    """
    html = urllib.request.urlopen(f"{url}/{page}", timeout=15).read().decode()
    html = re.sub(r'<link[^>]*app\.css[^>]*>', f"<style>{css}</style>", html)
    # app.js does the tab switching and sorting; neither moves layout, and
    # it cannot load over file:// anyway.
    html = re.sub(r'<script src="/static/app\.js[^>]*></script>', "", html)
    html = html.replace("</body>", PROBE + "</body>")
    with open(out, "w") as f:
        f.write(html)


def measure(workdir, name, width):
    r = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{workdir}:/w", "-u", "0",
         CHROME_IMAGE, "--no-sandbox", "--headless", "--disable-gpu",
         f"--window-size={width},1000", "--virtual-time-budget=4000",
         "--dump-dom", f"file:///w/{name}"],
        capture_output=True, text=True, timeout=180)
    m = re.search(r'<pre id="ds-probe">(.*?)</pre>', r.stdout, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1).replace("&quot;", '"'))
    except ValueError:
        return None


def main():
    url = "http://localhost:9090"
    if "--url" in sys.argv:
        url = sys.argv[sys.argv.index("--url") + 1]
    url = url.rstrip("/")

    if not have_docker():
        print("SKIP: no usable Docker, cannot start a headless browser")
        return 0
    try:
        css = urllib.request.urlopen(f"{url}/static/app.css", timeout=10).read().decode()
    except OSError as e:
        print(f"SKIP: no instance at {url} ({e})")
        return 0

    workdir = tempfile.mkdtemp()
    failures = []
    for page in PAGES:
        name = f"p_{page or 'status'}.html"
        try:
            build_page(url, page, css, os.path.join(workdir, name))
        except OSError as e:
            print(f"  SKIP {page or 'status'}: {e}")
            continue

        for width in DESKTOP + NARROW:
            d = measure(workdir, name, width)
            if d is None:
                print(f"  ?    {page or 'status':<9} {width:>5}px  no measurement")
                continue
            desktop = width in DESKTOP
            bad = []
            # Never, at any width: the page itself moving sideways. That
            # takes the header with it and reads as a broken render.
            if d["pageOverflow"] > 1:
                bad.append(f"page +{d['pageOverflow']}px")
            # On a desktop the table is expected to fit. Below that it may
            # scroll in its own box, which is the point of the box.
            if desktop and d["tableOverflow"] > 1:
                bad.append(f"table +{d['tableOverflow']}px")
            status = "FAIL" if bad else "ok  "
            extra = f"  table wants {d['tableWants']} in {d['boxWidth']}" if d["tableWants"] else ""
            print(f"  {status} {page or 'status':<9} {width:>5}px"
                  f"{'  ' + ', '.join(bad) if bad else ''}{extra}")
            if bad:
                failures.append((page or "status", width, ", ".join(bad)))

    print()
    if failures:
        print(f"FAIL — {len(failures)} problem(s):")
        for page, width, why in failures:
            print(f"  {page} at {width}px: {why}")
        return 1
    print("PASS — no page overflow anywhere, and the table fits on every "
          "desktop width from 1024 to 4K")
    return 0


if __name__ == "__main__":
    sys.exit(main())
