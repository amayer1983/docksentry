"""Settings that are switched on but not on screen (#2, @famewolf).

He had auto-cleanup running at 85% on three hosts and could only see it
on one. Four hours went into that, with @NotRetarded helping, before he
found it himself: the other two instances were in **simple mode**, and
the control lives in a block marked `adv-only`, which simple mode hides
with CSS. The setting was working the whole time. It just was not there
to look at.

His conclusion is the right one:

    If I have things enabled but can't see them in the gui how would I
    know to change them?

Simple mode is supposed to mean fewer knobs, not "your server is doing
things you cannot see". Hiding an option nobody has touched is fine;
hiding one that is switched on and acting is not.

Rather than keep a hand-written list of which settings are advanced —
which would be wrong the first time somebody moves a block — this reads
it back out of the page that was just rendered. A field is hidden if any
element around it carries `adv-only`, which is precisely the rule the
CSS applies, so the two cannot drift apart.
"""

from html.parser import HTMLParser


class _HiddenFields(HTMLParser):
    """Collect form field names sitting inside an `adv-only` element.

    A plain depth counter rather than a tree: we only ever need to know
    whether *some* open ancestor is advanced-only, and void elements
    (`<input>`, `<hr>`) never open a scope, so there is nothing a real
    tree would tell us that the counter does not.
    """

    VOID = {"input", "hr", "br", "img", "meta", "link"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # one entry per open non-void tag
        self.depth = 0           # how many of them are adv-only
        self.hidden = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        adv = "adv-only" in (a.get("class") or "").split()
        if tag in self.VOID:
            # A void element cannot contain anything, but it can itself
            # be the field — and it can itself be marked.
            if (adv or self.depth) and a.get("name"):
                self.hidden.add(a["name"])
            return
        self.stack.append(adv)
        if adv:
            self.depth += 1
        if self.depth and a.get("name"):
            self.hidden.add(a["name"])

    def handle_startendtag(self, tag, attrs):
        a = dict(attrs)
        adv = "adv-only" in (a.get("class") or "").split()
        if (adv or self.depth) and a.get("name"):
            self.hidden.add(a["name"])

    def handle_endtag(self, tag):
        if tag in self.VOID or not self.stack:
            return
        if self.stack.pop():
            self.depth -= 1


def hidden_fields(html):
    """Names of form fields that simple mode hides."""
    p = _HiddenFields()
    try:
        p.feed(html)
        p.close()
    except Exception:
        # A parse that fails must not cost the user their settings page.
        return set()
    return p.hidden


def active_hidden(config, html, defaults, labels=None):
    """Hidden settings that are not at their default, as (key, value).

    "Not at its default" rather than "switched on", because a disk
    threshold moved from 85 to 60 is every bit as invisible and every bit
    as consequential as a checkbox — his was both at once.

    Anything without a default entry is skipped: those are not settings
    with a defined resting state, and calling them "changed" would fill
    the notice with noise on a fresh install.
    """
    out = []
    for name in sorted(hidden_fields(html)):
        if name not in defaults:
            continue
        if not hasattr(config, name):
            continue
        value = getattr(config, name)
        if value == defaults[name]:
            continue
        out.append((name, value, (labels or {}).get(name, name)))
    return out


def as_text(value):
    """A value the way a person reads it, never a secret's contents.

    Callers filter secrets out before they get here; this only has to
    turn Python into something short and readable.
    """
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) or "—"
    text = str(value)
    return text if text else "—"
