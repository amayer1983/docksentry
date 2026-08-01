#!/usr/bin/env python3
"""Hourly snapshot of Docker Hub pull counts.

**What this measures, and what it does not.** `pull_count` is repo-wide and
cumulative, so the delta between two snapshots is the number of real image
downloads in that span. Verified empirically on 2026-08-01: a real
`docker pull` moved the counter by 1 within 25 seconds, while five manifest
HEAD requests — the exact call Docksentry's own update check makes — moved
it by 0. So our own checking traffic does not inflate these numbers.

What the delta is *not* is an installed base. An instance that runs happily
and never updates pulls zero times, forever. What the hours after a release
do give is a defensible **lower bound on the instances with auto-selfupdate
enabled**, since each of those pulls exactly once per release — and at
hourly resolution you see the *shape* of that: how fast it rises, where it
flattens, whether a second bump arrives when other timezones wake up.

Docker Hub exposes no per-tag counter, only `tag_last_pulled`. That still
answers "is anyone still deploying v1.55?" — just never "how many".

There is no history endpoint, so the series starts the hour this first runs.

Writes two files, both append-only:
  pulls.csv   — one row per run: timestamp, count, delta, hours since previous
  tags.jsonl  — one line per DAY (not per run): every tag with its
                last-pulled stamp. 154 tags at hourly cadence would be
                ~175 MB a year for data that moves slowly; daily is plenty.
"""

import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO = os.environ.get("STATS_REPO", "amayer1983/docksentry")
HUB = "https://hub.docker.com/v2/repositories"
OUT_DIR = os.environ.get("STATS_DIR", ".")
TIMEOUT = 30


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "docksentry-stats"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def all_tags():
    """Every tag with its last-pulled stamp, following pagination.

    Paginated deliberately rather than capped: the point of this file is to
    spot the long tail — someone still deploying a version from months ago —
    and a cap would silently hide exactly that.
    """
    out, url = [], f"{HUB}/{REPO}/tags/?page_size=100"
    while url:
        page = fetch(url)
        for t in page.get("results", []):
            out.append({
                "tag": t.get("name"),
                "last_pulled": t.get("tag_last_pulled"),
                "pushed": t.get("tag_last_pushed"),
            })
        url = page.get("next")
    return out


def last_row(path):
    """The previous snapshot, or None on the first ever run."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        return rows[-1] if rows else None
    except (OSError, ValueError, IndexError):
        return None


def _parse(stamp):
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def main():
    try:
        repo = fetch(f"{HUB}/{REPO}/")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        # A failed run must not write a row. A gap in the series is honest;
        # a row repeating the previous count would look like an hour in
        # which nobody pulled, which is a different claim entirely.
        print(f"fetch failed, writing nothing: {e}", file=sys.stderr)
        return 1

    count = int(repo.get("pull_count", 0))
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now.strftime("%Y-%m-%d")

    csv_path = os.path.join(OUT_DIR, "pulls.csv")
    prev = last_row(csv_path)
    delta = span = ""
    if prev:
        try:
            delta = count - int(prev["pull_count"])
        except (ValueError, KeyError):
            delta = ""
        prev_at = _parse(prev.get("timestamp_utc", ""))
        if prev_at:
            # Scheduled runs get delayed or skipped entirely under load, so
            # the gap is recorded rather than assumed to be one hour. A rate
            # computed against an assumed hour would be quietly wrong on
            # exactly the days worth looking at.
            span = round((now - prev_at).total_seconds() / 3600, 2)

    new_file = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp_utc", "pull_count", "delta", "hours_since"])
        w.writerow([stamp, count, delta, span])

    # Tags once a day: slow-moving data, and 154 of them every hour would
    # grow this file by ~175 MB a year for no extra insight.
    tags_path = os.path.join(OUT_DIR, "tags.jsonl")
    have_today = False
    if os.path.exists(tags_path):
        try:
            with open(tags_path) as f:
                for line in f:
                    pass
            have_today = json.loads(line).get("date_utc") == today
        except (OSError, ValueError, UnboundLocalError):
            have_today = False
    if not have_today:
        try:
            tags = all_tags()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            print(f"tag fetch failed, skipping today's snapshot: {e}",
                  file=sys.stderr)
        else:
            with open(tags_path, "a") as f:
                f.write(json.dumps({"date_utc": today, "tags": tags}) + "\n")

    print(f"{stamp}: {count} pulls "
          f"(delta {delta if delta != '' else 'n/a'} over "
          f"{span if span != '' else 'n/a'}h)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
