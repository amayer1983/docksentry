"""Reading CHANGELOG.md and comparing versions — front-end-neutral (#63).

This lived in `telegram_bot.py`, and `discord_bot.py` reached across into
the Telegram bot instance to borrow it — `bot._parse_changelog_entries`,
`bot._fetch_changelog`. But none of it is Telegram's: it fetches a file
and compares version strings. It lives here now, and both front ends call
it as equals, so neither depends on the other.

First step of the core extraction agreed on 2026-08-20 — the connection
is meant to be the thin adapter (render + send); the content runs once, in
the core. The Telegram-specific Markdown adaptation stays on the Telegram
bot, because that IS rendering.
"""

import re
import urllib.request

#: Where the published changelog lives — the same file people read on
#: GitHub, fetched at runtime so the message and the file cannot drift.
CHANGELOG_URL = ("https://raw.githubusercontent.com/amayer1983/docksentry"
                 "/main/CHANGELOG.md")

#: A `## [x.y.z]` or `## [x.y.z-beta.N]` heading with its date. The
#: prerelease suffix was once left out, so every `-beta.N` heading was
#: invisible and a beta version parsed as (0,0,0) — against which all 206
#: historical entries looked newer (#63). Both faults are gone here.
_HEADING = r"^## \[(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)\] - (\d{4}-\d{2}-\d{2})$"


def fetch(url=CHANGELOG_URL):
    """Fetch CHANGELOG.md from GitHub raw. Returns (ok, text_or_error)."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Docksentry/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return True, r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return False, str(e)[:200]


def version_key(vstr):
    """A comparable key for '2.18.0' or '2.18.0-beta.12'.

    A final release ranks ABOVE its own prereleases and both above the
    previous version: 2.18.0 > 2.18.0-beta.12 > 2.18.0-beta.1 > 2.17.0.
    The fourth field is 1 for a final release and 0 for a prerelease, so a
    beta sorts just below the release it leads up to.
    """
    m = re.match(r"(\d+)\.(\d+)\.(\d+)(?:-[A-Za-z]+\.?(\d+))?",
                 (vstr or "").strip())
    if not m:
        return (0, 0, 0, 0, 0)
    base = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if m.group(4) is None:
        return base + (1, 0)                 # final release
    return base + (0, int(m.group(4)))       # prerelease N


def parse_entries(text, after_version):
    """Entries newer than `after_version` as (version, date, body) tuples,
    newest first."""
    pat = re.compile(_HEADING, re.MULTILINE)
    matches = list(pat.finditer(text))
    entries = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entries.append((m.group(1), m.group(2), text[start:end].strip()))
    cur = version_key(after_version)
    return [(v, d, b) for v, d, b in entries if version_key(v) > cur]


def entry_for(text, version):
    """Return (version, date, body) for the exact `version` in the
    CHANGELOG, or None — what the version you are already on brought, when
    nothing newer exists."""
    want = (version or "").strip()
    pat = re.compile(_HEADING, re.MULTILINE)
    matches = list(pat.finditer(text))
    for i, m in enumerate(matches):
        if m.group(1) == want:
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            return (m.group(1), m.group(2), text[start:end].strip())
    return None


def report(text, version):
    """What `/changelog` has to say, decided once for every front end.

    Returns a dict:
        kind     — "newer": releases are waiting
                   "current": you are on the newest, and here is what it
                              brought (#2, @famewolf: answerable after a
                              /selfupdate)
                   "unknown": newest, and this version is not in the file
        entries  — [(version, date, body)] for "newer", newest first
        current  — (version, date, body) for "current"
        version  — the version we are running

    The *decision* lives here; the two front ends only lay it out, each in
    its own markdown. They used to decide for themselves, and drifted:
    Telegram showed what your current version brought and translated its
    lines, Discord said one hardcoded English sentence and printed raw
    GitHub markdown — @NotRetarded put the two side by side (#63).
    """
    entries = parse_entries(text, version)
    if entries:
        return {"kind": "newer", "entries": entries, "current": None,
                "version": version}
    cur = entry_for(text, version)
    if cur:
        return {"kind": "current", "entries": [], "current": cur,
                "version": version}
    return {"kind": "unknown", "entries": [], "current": None,
            "version": version}


def render_body(body, bold="*"):
    """A changelog entry's body, laid out for a chat.

    GitHub writes `### Fixed` and `**bold**`; a chat has no headings, and
    the two clients spell bold differently — Telegram `*x*`, Discord
    `**x**`. So the marker is the parameter and everything else is shared,
    the same arrangement `status_render` uses. Image embeds go: neither
    client inlines them, and the raw `![alt](url)` is noise.

    Telegram in particular chokes on GitHub's `**`, which is why this
    conversion is not optional there.
    """
    text = re.sub(r"^#{1,6}\s+(.+)$", rf"{bold}\1{bold}", body,
                  flags=re.MULTILINE)
    text = re.sub(r"\*\*([^*\n]+)\*\*", rf"{bold}\1{bold}", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    return text
