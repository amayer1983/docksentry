#!/usr/bin/env python3
"""What is still English in one language file, and what it must keep.

`python3 scripts/lang_todo.py fr [--json] [--slice 0:80]`

Prints one entry per key that is still verbatim English, with the
placeholders, backticks, leading emoji and line count the translation has
to preserve — the four things `test_translation_integrity.py` checks.
"""
import glob
import json
import os
import re
import string
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
KEEP = ("docksentry", "telegram", "discord", "gotify", "matrix", "apprise",
        "ntfy", "docker", "podman", "smtp", "http", "https", "url", "json",
        "id", "cron")


#: English function words that essentially do not occur in any of the
#: fifteen target languages. Two or more of them in one value means the
#: value is still English — which byte-equality against `en.json` misses
#: as soon as the English side gets reworded. A Dutch translator found
#: five such keys by reading; this finds them by counting.
ENGLISH_TELLS = {
    # Function words and contractions that are unmistakably English. The
    # bar is deliberately high: a word that also exists in German, Dutch
    # or a Romance language produces a false alarm, and a detector that
    # cries wolf gets ignored. "was", "one", "back", "for" and "use" all
    # had to come back out after flagging perfectly good German, Dutch
    # and Portuguese.
    "the", "and", "with", "that", "which", "your", "you", "from", "this",
    "these", "those", "have", "has", "been", "will", "would", "should",
    "could", "when", "while", "what", "where", "instead", "rather",
    "already", "again", "every", "only", "still", "than", "them", "they",
    "their", "there", "were", "into", "about", "after", "before",
    "because", "between", "during", "unless", "otherwise", "whether",
    "something", "anything", "nothing", "someone", "looks", "like",
    "example", "examples", "recognised", "recognized",
    # `doesn't` tokenises to `doesn` + `t`. These carry no risk at all.
    "doesn", "isn", "aren", "wasn", "didn", "hasn", "haven", "couldn",
    "wouldn", "shouldn", "won",
}


def looks_english(value):
    """True if this reads as English prose rather than a translation.

    Placeholders, code spans and URLs are stripped first — they are
    English everywhere on purpose.
    """
    bare = re.sub(r"[{][^}]*[}]|`[^`]*`|https?://\S+", " ", value)
    hits = {w for w in re.findall(r"[A-Za-z]+", bare.lower())
            if w in ENGLISH_TELLS}
    return len(hits) >= 2


def _de():
    """German as the completeness reference.

    It is the one file that was kept current, so "German bothered to
    translate this" is a better test of translatability than any word
    count. The two-word heuristic below missed every short UI label —
    `Cancel`, `Delete`, the weekday names, the tab titles — and two
    translators found them by hand before this did.
    """
    try:
        return json.load(open(os.path.join(APP, "lang", "de.json"),
                              encoding="utf-8"))
    except OSError:
        return {}


#: Languages that do not use the Latin alphabet. In those files, a run of
#: Latin words IS the signal — far stronger than any word list. It catches
#: what `looks_english` cannot: `help_detail_lifecycle` sat untranslated in
#: Ukrainian carrying an OLD English wording, so it was neither
#: byte-identical to `en.json` nor rich enough in function words to trip
#: the two-hit threshold. A translator found it by reading.
NON_LATIN = {"ru", "uk", "ar", "hi", "ja", "ko", "zh"}

#: Latin tokens that mean nothing about the language: OCI label names,
#: slash commands, env vars, hostnames, e-mail addresses, file names.
_IDENTIFIER = re.compile(r"[./@_\\-]")


def latin_prose(value, minimum=4):
    """True if `value` reads as Latin-script prose, not as identifiers."""
    bare = re.sub(r"[{][^}]*[}]|`[^`]*`|https?://\S+", " ", value)
    words = [w for w in bare.split() if not _IDENTIFIER.search(w)]
    if not words:
        return False
    latin = [w for w in words if re.fullmatch(r"[A-Za-z]{3,}", w)]
    return len(latin) >= minimum and len(latin) / len(words) > 0.6


def translatable(v, key=None, de=None):
    if key is not None and de:
        other = de.get(key)
        if isinstance(other, str) and other != v:
            return True                 # German translated it, so can we
    core = re.sub(r"[{][^}]*[}]|https?://\S+|`[^`]*`|[^\w\s]", " ", v)
    w = [x for x in core.split() if len(x) > 2 and x.lower() not in KEEP]
    return len(w) >= 2


def todo(code):
    en = json.load(open(os.path.join(APP, "lang", "en.json"), encoding="utf-8"))
    d = json.load(open(os.path.join(APP, "lang", f"{code}.json"),
                       encoding="utf-8"))
    de = _de() if code != "de" else {}
    out = {}
    for k, v in en.items():
        if not isinstance(v, str) or not translatable(v, k, de):
            continue
        cur = d.get(k)
        # Two ways to still be English: never touched (byte-identical), or
        # touched long ago and left behind when the English was reworded.
        if cur == v or (isinstance(cur, str) and looks_english(cur)):
            out[k] = v
    return out


def main():
    if len(sys.argv) < 2:
        codes = sorted(os.path.basename(f)[:-5]
                       for f in glob.glob(os.path.join(APP, "lang", "*.json")))
        print("Sprachen:", " ".join(codes))
        for c in codes:
            if c != "en":
                print(f"  {c}: {len(todo(c))} offen")
        return 0
    code = sys.argv[1]
    items = todo(code)
    keys = sorted(items)
    for arg in sys.argv[2:]:
        if arg.startswith("--slice"):
            a, b = arg.split("=", 1)[1].split(":")
            keys = keys[int(a):int(b)]
    if "--json" in sys.argv:
        print(json.dumps({k: items[k] for k in keys},
                         ensure_ascii=False, indent=1))
        return 0
    for k in keys:
        v = items[k]
        ph = sorted(f for _, f, _, _ in string.Formatter().parse(v) if f)
        print(f"--- {k}")
        print(f"    behalten: Platzhalter={ph} backticks={v.count('`')} "
              f"zeilen={v.count(chr(10))}")
        print(f"    en: {v!r}")
    print(f"\n{len(keys)} von {len(items)} offenen Keys für {code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
