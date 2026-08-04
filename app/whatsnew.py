"""What changed in the version that just started running.

Most people update Docksentry the way they update everything else — `docker
pull` and `up -d` — and nothing told them what they got. The self-update
path already announces itself, but that is the minority route; the ordinary
one was silent, so new features shipped and sat unused because nobody knew
they were there.

The headlines come out of CHANGELOG.md, which ships in the image for this
purpose. Parsing it at runtime rather than baking a summary at build time
keeps the two from drifting: there is one source of truth, and it is the
same file people read on GitHub.

Nothing here decides *whether* to say something — that is main's job, and
the distinction matters: on a first-ever boot there is no previous version
and announcing "updated" would be a lie.
"""

import os
import re

#: Alongside app/ in the image; falls back to the repo layout when running
#: from a checkout.
_CANDIDATES = (
    os.path.join(os.path.dirname(__file__), "CHANGELOG.md"),
    os.path.join(os.path.dirname(__file__), "..", "CHANGELOG.md"),
)

#: The repo it links to. Kept here rather than derived from a label,
#: because this is Docksentry talking about itself, not about a container
#: it manages.
REPO = "amayer1983/docksentry"

#: Long enough to be worth reading, short enough for a chat message. Four
#: headlines covers every release this project has actually shipped.
MAX_HEADLINES = 5


def _changelog():
    for path in _CANDIDATES:
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return f.read()
            except OSError:
                return ""
    return ""


def headlines(version, text=None):
    """Bold lead phrases from `version`'s section, in order.

    Entries in this changelog are written as `- **Headline.** prose…`, so
    the bold run is a written summary rather than something guessed at.
    A section with no bold entries returns [] and the caller falls back to
    the link alone — better than quoting an arbitrary first line.
    """
    text = _changelog() if text is None else text
    if not text:
        return []
    # Section runs from its own heading to the next one.
    start = re.search(r"^## \[%s\]" % re.escape(version), text, re.M)
    if not start:
        return []
    rest = text[start.end():]
    nxt = re.search(r"^## \[", rest, re.M)
    body = rest[:nxt.start()] if nxt else rest
    out = []
    for m in re.finditer(r"^[-*] \*\*(.+?)\*\*", body, re.M):
        line = m.group(1).strip().rstrip(".").replace("`", "")
        if line and line not in out:
            out.append(line)
        if len(out) >= MAX_HEADLINES:
            break
    return out


def release_url(version):
    return f"https://github.com/{REPO}/releases/tag/v{version}"


def summary(old, new, t=None):
    """The message body, or "" when there is nothing worth sending.

    `t` is the translator; the framing is localised, the headlines are not
    — they are release notes, and translating them would mean translating
    the changelog.
    """
    lines = headlines(new)
    head = (t("whatsnew_title", old=old, new=new) if t
            else f"🚀 Docksentry updated: v{old} → v{new}")
    parts = [head]
    if lines:
        parts.append("")
        parts.extend(f"• {l}" for l in lines)
    parts.append("")
    parts.append((t("whatsnew_link") if t else "Full notes:") + " "
                 + release_url(new))
    return "\n".join(parts)
