#!/usr/bin/env python3
"""What is owed a reply — every comment since the maintainer's own last one.

Not a status table. A status table is what failed: twice I looked at a
thread, saw "last comment: LeeNX", noted it, and moved on without
answering. Twice a reporter had to ask whether I had missed it — once by
tagging me directly ("Did you miss this?"). So this prints a *queue*, and
an empty queue is the only clean result.

No local state, deliberately. The obvious design is a database of "issue N
read up to comment M", and it is the wrong one: it needs syncing, it goes
stale the moment a reply is written from anywhere else, and it adds a
failure mode where the record says read and the thread says otherwise.
GitHub already stores the watermark — the maintainer's own last comment in
the thread. Everything after it is unanswered, by definition. That cannot
drift, survives a new machine, and stays correct when someone else replies
in between.

A thread the maintainer has never commented on counts as entirely unread,
which is right: nobody has answered it.

    python3 scripts/issue_inbox.py            # open issues
    python3 scripts/issue_inbox.py --days 14  # …plus recently-closed ones
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

OWNER = "amayer1983"


def gh(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"gh failed: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout or "[]")


def owed(number):
    """(title, [comments after the maintainer's last one])."""
    d = gh("issue", "view", str(number), "--json", "title,comments")
    comments = d.get("comments") or []
    mine = [i for i, c in enumerate(comments)
            if (c.get("author") or {}).get("login") == OWNER]
    after = comments[max(mine) + 1:] if mine else comments
    return d.get("title", ""), after


def main():
    days = 0
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])

    numbers = [i["number"] for i in
               gh("issue", "list", "--state", "open", "--limit", "100",
                  "--json", "number")]
    if days:
        cut = datetime.now(timezone.utc) - timedelta(days=days)
        for i in gh("issue", "list", "--state", "closed", "--limit", "50",
                    "--json", "number,closedAt"):
            if i.get("closedAt") and datetime.fromisoformat(
                    i["closedAt"].replace("Z", "+00:00")) > cut:
                numbers.append(i["number"])

    queue = []
    for n in sorted(set(numbers), reverse=True):
        title, after = owed(n)
        if after:
            queue.append((n, title, after))

    if not queue:
        print(f"Inbox empty — every one of {len(numbers)} threads has been "
              "answered since its last comment.")
        return 0

    total = sum(len(a) for _, _, a in queue)
    print(f"{total} comment(s) awaiting a reply, across {len(queue)} thread(s):\n")
    for n, title, after in queue:
        print(f"  #{n} — {title}")
        for c in after:
            who = (c.get("author") or {}).get("login", "?")
            when = (c.get("createdAt") or "")[:16].replace("T", " ")
            first = " ".join((c.get("body") or "").split())[:96]
            print(f"      {when}  {who}")
            print(f"          {first}…")
        print()
    # Non-zero so this can gate a "we're done for the day" check without
    # anyone having to read the output.
    return 1


if __name__ == "__main__":
    sys.exit(main())
