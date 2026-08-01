#!/usr/bin/env python3
"""Optional lightweight Web UI for configuration and status."""

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import subprocess
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from config import PERSISTENT_KEYS
from link_resolver import LinkResolver


# Static frontend assets live as real files under app/static/ (extracted
# from the old _BASE_CSS/_BASE_JS Python string literals) and are served via
# the /static route. Cached in memory after first read — they never change at
# runtime, and a `?v={VERSION}` query on the URL busts the browser cache on
# upgrade.
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_STATIC_TYPES = {".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".webmanifest": "application/manifest+json; charset=utf-8",
                 ".svg": "image/svg+xml; charset=utf-8"}
_STATIC_CACHE = {}

# Serialises every update check triggered from the Web UI — the global
# "Check Updates" button and the per-container one alike. Both write
# config.pending_file, so two overlapping runs raced each other and the
# loser's result was silently overwritten. Two quick clicks on the global
# button were enough to hit it (#50). Non-blocking acquire: a second
# request is refused, not queued.
_CHECK_LOCK = threading.Lock()


def _read_static(name):
    """Read app/static/<name> (basename-sanitised), cached after first read.
    Returns b"" when the file is missing."""
    name = os.path.basename(name)
    if name in _STATIC_CACHE:
        return _STATIC_CACHE[name]
    try:
        with open(os.path.join(_STATIC_DIR, name), "rb") as f:
            data = f.read()
    except OSError:
        data = b""
    _STATIC_CACHE[name] = data
    return data


def _e(value):
    """HTML-escape a value (including quotes) for safe insertion into HTML
    content or attribute values. Always coerces to str first."""
    return html.escape(str(value if value is not None else ""), quote=True)


# ── SVG icon set (Lucide-inspired strokes) ──────────────────────────
# Inline SVG so they pick up `color` from the parent (currentColor) — we
# want Pin to be red when active and grey when inactive, and the only way
# to do that is *not* using a color emoji like 📌.
_ICONS = {
    "refresh":   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 0 1-9 9c-2.4 0-4.6-.9-6.3-2.5L3 21"/><path d="M3 12a9 9 0 0 1 9-9c2.4 0 4.6.9 6.3 2.5L21 3"/><path d="M21 3v6h-6"/><path d="M3 21v-6h6"/></svg>',
    # Single circular arrow — deliberately distinct from the two-arrow
    # "refresh" so Update and Restart stop sharing a glyph (#46, @LeeNX).
    "restart":   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36L21 8"/><path d="M21 3v5h-5"/></svg>',
    "pin":       '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 17 .003 5"/><path d="M12 17a5 5 0 0 0 5-5V8.4l1.6-1.6a1 1 0 0 0 0-1.4l-3-3a1 1 0 0 0-1.4 0L12.6 4H7a5 5 0 0 0-5 5"/><path d="M2 22 22 2"/></svg>',
    "settings":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2"/><circle cx="12" cy="12" r="3"/></svg>',
    "alert":     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
    "checkmark": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
    "x":         '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>',
    "search":    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>',
    "broom":     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 13 4 20"/><path d="M5 19a3 3 0 0 1 0-6"/><path d="M11 13 22 2"/><path d="M22 2v6"/><path d="m11 13 6 6"/><path d="M17 19h5"/></svg>',
    "arrow_up":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 7-7 7 7"/><path d="M12 19V5"/></svg>',
    "calendar":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="4" rx="2"/><path d="M16 2v4"/><path d="M8 2v4"/><path d="M3 10h18"/></svg>',
    "package":   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m7.5 4.27 9 5.15"/><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/></svg>',
}

# Inline-flex helper that pairs an SVG icon with text — used in badges
# where we want a small icon glued to a label.
def _strip_emoji(label):
    """Strip a leading emoji/symbol prefix from a translated button label.

    Labels like "🟥 Stop" / "🔁 Restart" are right for Telegram buttons,
    but the Web UI draws its own icons — next to a styled key the emoji
    doubled it ("double red square" next to Stop, "the old icon" next to
    Restart; #46, @LeeNX), and inside a title= attribute or a confirm
    dialog it's just noise the browser renders raw. Strips leading
    non-letter symbols only, so any language's actual text survives
    untouched. Used well beyond the legend now, hence the rename from
    _legend_word.
    """
    import re
    return re.sub(r"^[^\w(]+", "", label, flags=re.UNICODE) or label


def _strip_md(text):
    """Drop Telegram markdown markers from a string bound for HTML.

    Every user-facing string in this project was written for Telegram
    first, where `*head*` renders bold and backticks render code. The
    Web UI hands the same strings to a browser, which shows the markers
    themselves — the groups page read "the first container is the
    *head*" with literal asterisks, and a rolled-back update's log
    turned up wrapped in ``` fences on the history page. Same root as
    the emoji doubling in #46: Telegram formatting leaking into HTML.

    Markers are removed and the text inside them kept — deliberately
    NOT converted to <strong>/<code>. History details carry raw
    container logs, i.e. arbitrary upstream output, and turning parts
    of that into markup is how you grow an injection hole.
    """
    if not text:
        return text
    import re
    text = text.replace("```", "")
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*([^*\n]+)\*", r"\1", text)
    return text


def _web_translator(language):
    """`get_translator`, minus the Telegram markdown.

    Wrapping here rather than in i18n keeps Telegram's own formatting
    intact — the bot and the Web UI share every translation file. And
    wrapping at the source means a future string with a backtick in it
    can't reintroduce the bug: there is no call site left to forget.
    """
    from i18n import get_translator
    base = get_translator(language)

    def t(key, **kwargs):
        return _strip_md(base(key, **kwargs))

    return t


def _legend_word(label):
    """Legend key text: emoji-stripped and first letter upper-cased.

    The legend mixed "Update"/"Restart" with a lowercase "auto" (#46,
    @LeeNX). Deliberately `s[:1].upper() + s[1:]` and not `.capitalize()`
    — the latter lowercases the rest and would turn "Auto-Update" into
    "Auto-update". Only the legend uses this; the badge text and the table
    header keep their lowercase "auto".
    """
    s = _strip_emoji(label)
    return s[:1].upper() + s[1:]


def _icon_label(icon_key, label):
    """Return an inline SVG icon followed by a label, both inside a span."""
    return (f'<span class="icon-label">'
            f'{_ICONS.get(icon_key, "")}{label}</span>')


_LOGO_B64 = "iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAYAAADDPmHLAAAbiklEQVR42u2deXxU5bnHf897zsyZyTJZIOwCUlewbiCXKjbiDsUFbyf2aluv1WoX0SoXsVjvkLq2FhVQLFQvUhDbCbggBhAhhB0NgkACBEhYEiD7ZJn9nPPcP+bMZBIQrXVheb+fTz5JZnImM+f5Pb/ned7zTgJIJBKJRCKRSCQSiUQikUgkEolEIpFIJBKJRCKRSCQSiUQikUgkEolEIpFITkrotH3lHhYAhPWdiXwypRxOdbF7WMDDKpjpmLngYRVerwIwSQc4JWCCGwIDQcgnPfme9D98dq5iS79BOGwKh+s/bPrfS0o7HOplBQDghgkilgI4aWLOhALL2vPISLpHTX9i12DhcI2EYr8RpF4qUl02kQZwKGiSaWwhhJZSJFBY99H/fYpF+YEOYihdScBKE/n5phTAyYL75bSMgTcNg+oYDUW9nlTH+eRMARsAokGATF2k2kGKqgoHgWwAIgBH/fuEGfkI0cDC4J7ta1tevLFROsCJZu2eSdQpGwlgpNy3pIfaZUAuHBmjSFGvInv6GdDsgG6C9SCDoBNBgCAgCCJVIxLERDBBYBJCCDsE2QGYgBkK1ZIRXotI2/uoq1pZ88R/7AM6lQSPRyB/Eh91uxTA1xx0LwRKj67nsaaNGDdO0TKG3b6FXN3PAwPQI2AzYoDITASdKPaqCYAgKGl2kBBEREzx2wkmERgAkQ2KsANkA4z61gO04a3zD8+8P5D4nckUsYo68MnUN6gn9qjmEcBVAoOuYuSRgTzEavqwh52uy/6zV8u04RUA2k+0b59gEi5EwgaMiAGCCkHCCj46BJ/ISgAr8NZAaIlAxMVAJkwzaBoiKgTZNC2iK7HmkBPpQ909Rf1qClYewgiKJPUiIncSRPEJPmKqJ5wjeTgWjkkwQGQCMZvP+PWiLNN1znBhTx9JzvSRpr9+P4Cr4GGBfCscZ7gUMAwQKVawiRJB7xh8IoAJTCLmBPG7IeLaSIiBBAmFBATIZDPHJRJjIrMAkan0//7r/Z7+QT+z5cElht66RBzYua6KqLEYMOONae5KKMUrYSKfuINoZQn4fDJ+t3qA6eiZS46MUSD1SmFP7Q5hA2yA6Tu8vOWPPa/DfZaIZ1IUAFz5dfuFM6MvG2EjlsmUsPukzEc869V0jUgRIAKDYtoAYsJICAFgskNwKHyk+k5HHwDG4BK2bQKAIdD7vNGyzNbLdQ2HY0ZihkJ1ZIbXcshfqLfuL6p64PI90gG+TI0fPER1XTnzIqR2ux6KbRQL+6XCkekkInA0BDMSMoiCOpHLBmIBEGMmovHXkvb49mFCtaWCDY4FktozP6F4BienOMW+TVh+p6/jdYIYTAo5+7xSNrzqtwPXbhpC8d8LRTSDojDNkBmFEKpid+QIh+NWooxbba4u4bMLmkuII4vMlqale+49ZysAQwqgU/cOENKvq3yf0nreAEUD2ARHA+Bwq24Fy6rlpIAgiCmKYW5nxlWTroAzazQp2g2waecBBJg6g0Dtlp9kd1aKI24IImbzlhCI2nuB2G3ximGCSbVnUI9zV/b/R9sOmPpiI1C79OCcp1eBDF2oEBBQiSDIhGkGYBIAoWiaSNWuECquQHr2s+fPb9xQv2LedXXTH/CDGd91s0gnhgDAAETaxEN7lZSu/Tnij4CggEjEEpkIZNVNIoaiEPRQPbPepjgzz4TNBugGOBrkDvUeydYfe5j22wESBFuGHaQIEvES0F7720XSXjkgBCA0kLADrAPc5q+EUNOEpuVAb3+MpMdiwGQiYSg22Dka9DeXvNm3Kv+XjWCm71oAJ1QTSEDIygrFyk3EUpk5cUYBgqGDVGcOKWqOGQ2Z0MMGwRrzjpY3ob1rO0rzRO0aiflMTGjUSQwJJyAwIqZpRoRJBKGkpZ4JA4AOtoZMThQfAgurjSQCKcQwwQERjrIsAcfqAtqvzsVqNeIJQugYPwKbURMchZXXakczOaq+dDQbskRFVpcOU1AsSB1dwAoiUVLlIEBACJCljahpgkEkRHs/0S6g9uMBKAqBBAQyZBN4nFaQrPAnBS3ZU5OLeVwqyaWUqEP4uUMPyADDBDGTqqnCoQg11bo/DAZgWBOisB6GkwIZmwZiYuD2IMerVFLgrWNFUkMpACiJfiRDCuDYNYC+dHfSnut8tEEkjmdr2GcQyGAGCUeKImwAQi21qG+aR1GlRaRn3Ub2lAtECqlsABwTg04iVvIJSQGnhLV3cIlEu0FHl5a4KBSr70gLBUgK4Jjpz/HP1DGT0Sn7k3yd2p2Djv5ZBtgESCFHiiqYgXDzdgq0zFAObPjnkWl5ddZP/rHfX8uGclvWbYozdSTsqYNEqrCxDiACM1YjIIiQaBZFhyavPchkZXvi6yQRKAREialFrgN8gQOIWOEnOrYdEJJFQFaTxx2qAphNCFUlp0Mg1GZyoOED1Wz5m6voxaV7lkwLA8BAL9sBoMyN6H6i9QDWA3jse3/dOhRpXccoTtdNipZ6nnBCcATgKExQTAyCkoaKToEXx3AKsiYIFoBLCuD4BpBUsa2YduzeueOkx+1+YDV2iqaSZhMcaG3j5mav2lbzSv0zF30KALVJj1+Wl7R2DyC3iNXiEaTv/dWFcTH84bzXy34g0nNuI815sy0ltb+iQZghgHXoRCYJEiI+HlKSAITVJ4ikKUIRiC0OuKQAPrew0zGHtfZMO6ocxJzfBNiE6lSFpgoz0FyDYMss07f7tZa/XLM3vm7f48WK2+w9sn5DYYqqdhKanaAIQFGghvfveqR4BG12e1kpQAFyc9xUPIIiO+8ZWAyguPtPx03sdu0vfygycn6saM7R9nRnN4aAGQSYoSeViM5OEHtZBFYIUAVghu2yBzjOKNie5ckToPVNwhHiXzAbsGkq2W2CA76DZpvv1eie1a/75/48luyxrV0KiCLqjIb+tpysEWYroGiA6ojVZVUDjKbU7MSTyMsziuPL094CYYnBXzN38mIAi8/xLOyaNejS0Wpa+s9tqparZmqqEQI4Cl2QKWIXIDsIgOOCMAAIzSnXAY4ZeyZO9vhY995+qQAEYqsjZ2aDVE0lzS446DuItqapXLLk9eYPftMEILbBE5NM5JGRW8RUDEAoHEYQUUSgmyZU6+qfSSaEYnbeY4CYBPOQEIPbCwE3UEBUD+ANAG9cPmvzxSLc625Kcd3uyHB0J0NAD8EgmFBICEoaEUWsEaTUcFA6wHHznzh5OYaTbJ8BNkCKKlJSVA4013NT9TSzbPnLLQW/bAQRBnrZXgYYcMMEJgGTJom60tgCkwA7bE7YTB02mx2wOwHBgE0DWLAKAFkDIJiZj71EEdtjyMxERMzMChFtAfDQYM/CJ/mCC+5wdMm535mWNlAoAnrsegCTtb9AsZpAMxySAjh2BxDfjt2e6O2NAZsgInKmqwi3htBcPUMc2vx844ybqhMWwcSdGzsAKAMiAODfuXFOdI9tlTDICChEUWdcdqbg+todADBzCEVnfuGwQvjd68sGEFGFJQhFENUzMBUDB/716kkLbtayezzsyMi8XLUBkQAMgsmqEIIVAOlpsgk87hjIsDop6/SCTNJSVYIO+OveJd/uJ5omX7Hdsno78hHtOXH9JdSv4XkCGQALshErBFNJYRv8obKKX/R+oNv55w/RemffSyHW7aoQKgE2BayqUPXaI0/smYat9y+vmNytp+tiioqoQqwQSE91mvba/Y0v/enac94HgFnLttwweujZ7z44sunDkt2HZxBRYazdYHueEJEVeefPBzD/xvm7Rjm7dn9Uc2XkKkLAjMA0kzaJuAsKREFM2CwF0HnQixmAAcWuksMpONiwRQSrJzY9c9HiRHPnhokCGAAxXDty1G7ZV3MUIAVQlFjNtWcARq0/BwDU9MyBKT0zbzFbAc0O2ESsCXSmAOHWxukAtqZl5/ww+8y0IUYLYLcBMICsrkCoWV8Yf3b/cVbPcV1dKVpXV8pNZ/bMvGlfTdP6VZsr/phHtIQI+GUJ22ZeJqJLfnxuIYDCmxfsuNmZ0z0/o3fWxf464eyRAgNEXHAC7As4AfcEMtgal8mZoVKkxUe+w081PXXTNKAsAmaBSWjf8++NJY9NMaIIIQwDBoRpLeKaJkJCUYh9sZ8hP7chjCCiHIZqqiaIhGkaUIQwowDAJvsCPjNsBoQeVUxFJaEHm2EzDT0CAGM8C7r16Oq6LKob/O6G8kcu/V73kd/rmX39z264dPGZ2/e/Nvrx/xs/cwj5vF5WSgeWKpMGDdKJaCGAhZfO2zWmZ2bq4x8s3Kci15N2/91XZoSaWv2zHx7jw7GuZJ2OS8FsskGqZiNFgP01C1Bb8qjv1dEVAAFurwJKfrMH0+CmTWK0h/mfWoXTzIZmRgChCggFEBCwZwJmgLoXFRWp/9smXKldoenN0BwaoAoBASA9HQjWaw5mpglbAl3SuwgtqkLTbAKCAVcG0KggFQCuurTfgPQULaM1GG7Ku3LgdAAveYu23n3NkLP+MnxQ33s/nv7QsKnXDv3PvDwqB2DkA3j0jeWDfnBO3x8M6pHeO6gI89W5Yzel2VUFRJkfba96YjbwQhGzMoKONYmcTmMgBFGqpqC1qVxpPfL7xj8PfDs+0nkAc9IkAGDVyhRTEPGm+xHdBAAPrt6WozinRllErLbOZAJrKaQYrYGKEbeN0G1TypecZQS7mCGKaoqppNiIVCIEHabW2NxWRkT80Mr9c+t3OTZTmFklKELADDpgCza3bAUzae9/nKoKYjbhH+B+zrnXOyFKRLP+8LdFG+69+Yp/nNMr68Lx7ss/QsT7o5tyB194Tp/sX3XLShueam8/1a2BUKtmU1W7TXXmpDtaAQArT/cSkOtRFCPEaKqZpm6d/XjDwgmtbu92+wT3IB4qRDSfGfn5nQ86S7vsN7/LGj7wjLSrLkrT+rZ9NjOSnu6MQnW6FMWh2lSKREwRUe22mnXld9r1YDjL9C0XTmHaQLoeifr8rW3BlOZA22K/oHOmvd1lylX9Xjje08xcsTlkDQKpF17SUyUi3r59u/2CCy7YcbDKe8PTY28o6tc987wn7h65vntWWioANLYEjxysbX57b1XD8kO1tbsmv77uwKLpv1gwoGf2tcFwOCaA70oBJwr9PEWOfo8suySpHHSYlcdNfe/M+StLR63ffnBCacWRWRWH6tfWNLZW1LcEWv2hKH8dtAYjZmNbsPlQY+veyiO+1aUHGmaX7K15rHBz5RjPvFUXAn2cwC2ZvrZgcyRqmH/xrh7MzORhFjNKSmwA8OybKwa3BEJtzMx1Pn9N8dZ9Dwy7Z3J2J7Wrtb62fczMizbu+iEAeONvRj1dHWB//ojQ9Z4l+4d6WSnIIwO97ne+s3bHlQO6Z1+fmablZqY7BrpSNGfn49qCYTMQijQ0tfjrhKB6fyDUyiRqI1Hdb0BpCYRDfsEcTXXaIgxm0ySK6rpiGKywYEd2aqorrEfT01OcGWRyF0URPVRV6dHF5RzQ32kfHv89Iy/ujwd+tMvX2Oz/GERBmypSLz2r1w1EtImZFRoyJMolJTYaMmTTtbuq33alapdMe2vFzS+Pz6u0hlm1oLRUoKzMqFT6XOhKcfRtC4b9H22p2A0ApaWTGKc7V0/8qDcz09N/X3vBvpqWfcnZqZvMRxpb9u8+ULdoQ+m+ZwvX7/zprPc+ueLxqe+dOdA9OftrFrN24++9OS/NW3Xh2ytLR67fWjmurLJm7sGaptLWYCTxnOqb/Q3XTpiREV8djH94Zi/r+/1fT88CgJKSElvczZhjq43ry6qeYGaz4lD9OgDEzHSahz52AsY8v7QbmOnBl94/2+cPMzPrB2qbVq/dtu9/3nj/k6HIcX/hEpoiCMysfoUPhZmFEOK4m5HGTJzRc+G6bTd9tvvQlOq65oqVWyoKphSWa8xHW7jH4xHJXzOzkuP2pNX42iqYmVds2ftIsjBOZwgA3M8ty5gwoyQDgDhwpKnUZObFG8t/1XFSZMHMalFRkRo/cQUrPrumur7ln9sqap9xe15OY2bCv5VViWwWljBUZlaEOOoh1blLPx35lPej3nEXSHaDRPCt5wwAa7btn8bMXNPUeujhyUuyO//sac2NYwu1+15a3RcAPtt96C1mNssP1i3zehNBEEVFRSrA5PF4BBHhhTdXD6j1+YNxWy4pr5oaX5//Jp5jPJOZWaXPXc5gShJOwgXWbNs3PhLVmZl5ycYdd36Tz/Ok5e4Xlg0AgPfWlP6UmbnZH2x7/JUlZyDJTglAeTlrHg+LguLN18ebeGY29lTXF3+LJ5a8X+JvCr347sfnlu2vnRMX6Sc7Dz4vg/85fcBdf17UY+yUQu0ejze7tqmtnpl5xae7x3mKitTlJeV/W/rxrp8A0OJH/em1d9N37KvZwMzc1BqILF5XmmddmPnWT27czv/sLeqxfHOFe81nFePLq+sXtQbCQWbmFn8ouqF0/0QZ/OPgnrzO+fCr63oDwOote2cyMx+s9VUCwLptFY8yM1fVN+/4tLz6qdkfbBoSP654+4Gh81eWng0AJVxis04wfcsCUAHgkx0HX0meYPzBCO88UPvhjIXrh8f7GBnp43D/5Pd7MzP9/tXCgb7WQJiZeeGabfcCQFWtb3/8xLYFw7zvcOPmzeVVz8xatPFiALbvzL+spvGOx97MamgO1Oi6YWzbe2jWqi17Hpz6j9UXod9dmTLzv+w04PFmP/bm6iwAKNpUPoOZ+WBN06HcuzyORatL88xY/IPMbCbWCZi5ur75yJ6q+iWf7j3y3HvrSt1nnTVWszrsb9wJSqyVwDVbK59jZi6rrFmY1DqKp+YU94xlvuz4v5BcT5H6s+e8fZmZRj8yo+uBI40HmZnXb6ucBgDbKw5/wMxsGEbEYNYN5ggzG52XdgvXbr8PAIqKjj9nJ8/rX4X4409/Z82VoXDUqPf56x9/bVE/axJQ7np2cf+xUwo1Gdl/ZVHoqQU9x04pdAHAG4s2jGj1h6LBcJTnF2299iHPrMyqOl8dM5u6YehG7LOpG4ahG0bUMIwQM0cP1DTt737duFRrnqcvsHDlqwgh3my6/2dWj8aWwJHmtmDgjUUlI+JlYbRnYcrtT75zRvJrk3yZZtDjtd/53II+8cAVrt1+n8nMdU3+I//lmdF1duH64fXNfoOZ9bgIDMMwY5/Z5Jgr8LptlVNijSEfsz8YO6XQ9ac5y89NFoL1cczSEb/4A4DigplTuKFPdV1zVUNz4ODU+auuSHaF2z3v9L9RZv9XcwG3x9vD7fGmxbvmlSXlDzEzV1bXbQCADzfuzAuEo8zMRrsI2Iw7AjNHw1GD5y7++LbkOp0c7LFTCrVVW/b+s/xA3ZQn5y7vl3y/IELSauBRLsLMVLC6dNC6bfvW7zpQ9+LwXz8b61uKilQAuNNT6HJP9PaW2f9v9AJjPAv6JI9Y763Z/t/BiMF7q+s/AoAVJeV3+tpiC4G6bkQNjgnAEoHBzEatr63lZe+aiwGgKGndPbaIA8xbtuUSax2hpazy8NylJXvG/Hbau72O5QA3j38t/R8rtl0xxbsqh5lpZmHJsGffKuqfJIr4dla67cn5/SyXkMH/qhPBjx57M+vWZ97ukiyCWYUlw+qaWg/vra5fDgDzlpZcXdPYVmf1fhHdMIwkEejMzDWNrYdemLd8UGcniI9ln5ZXz0puIBtbAi0Ha5u2HzjStHR/je+dA3W+RXU+//aKw43VhRt3PjPDW5KRPM9by76Jre0/efq97nc8uyhLZv/XIIIxngV93B6vPbm2XnSXJ3Pn/rq31m6tXPVO0ebMJ2cuPbO8qnZ5PIBGkhB0g6PMzLWNbTVzl35yRXLAPJ6Ytf/h1Q/PbmzxB5g5HO8fOlNd17xlyYYdIzpPEJ1Lw12eWY64c8ns/xq4cWyhlnxC49YNAB9+smvUvBVbPL+evigLAK34ZPcjddYScpIj6LphRJmZfW3B4Eef7LynU9NnXamrnGIdE4ofHIqavLuqbsM7K7bd3rnz//zeZWHfXE+RKiP3NbrAXZ53Mm99bHaX+G3xK26JTPQWpcX/rsujz83ps7n84F9qG1sbkoRgWoGNGsy8fe/h2b96fkG3uAhKSkpsv/3TvF61vrZGa7NHcOf+2gVvLdsyGtbfLhKxvQbiuOPrxAU93RO8GTL7v6mpYLK3w7awZDewhJH4/heeeb2KP9szvvJww6YWf+goS6/1te5bUVJ+Z/LjLf9413Mby6rmPDZ90YDENBALvPJFz++Wh2Zl3jx+Xi9Z978hJ/B4PMI90dv7i+y1szsAwHTv6sFrPqt8fE9V/Yp6n78+GGlfOKyq9W1bWVJ+BzDQDrjtby3bNCneJ1ibNb8wmMMe9jpHPT6338kW/JNOpW6P1x4EeixCadUX/fcOZqaVK1cq11xztW6a7Xsub31sdpcxl599Qe+eXc7r36vL99Oc9qGhiN6/3heo9PkDs2qbAgNC0ejaX4y87D2v16vk5eUd9y1cbi8rvo0FfSNpOQeL80foJ9P5POkalYL8vMh14/7eMCptULdC4AiO85Yqir3pUk+azwUAk4ga3gWKEfuwGGxbUvLm92yKvW+GK72uuaaxGQBKS93H3a3r8XjE2k0FvaNp/sPF+Xk6JN+Oa93omeO65ZG3zvgqTsbM5PV6laKixIbQr3gxiGnUuLn9Lh//Wrps+r6DpvAWzzuZYyYu6Pl1PabH4xFer1fxMisez/FF4fF4xKhx8/vl/sabdjIHn05uERDf8tCszLCmpi3588+q8c2/u5YAsNvtVVp6GWdodn/DwufvbT3mv4+RAvj2RHDz+HfTgUB6c8qu2uL8fB3fzFutCQAP9HjtfVqN7hms1Be8mBfEd/S2bimAzln5sNcZcCI7xYa6gvy8yNccGALA1437e6owyFUfCNRvmnl/9GQP/qnUtMRE4PUqvo1KFyUSCC2Z9rOWr+txAWD4WG+OPU0XK579r9qT1e5PZQF0KAvXjZuTY0RVWjE1EayvkqkEgAffN8PmcDh6EuvBNdPuqTslR6pTkWsneDOigXB6WNUaNsRq9b+c9bkPzcrUWbhSbNSwbPLP/afsTH2Kvi4efN8MW0pKSlc2NH3NVHd90n+fOK4bDL5vhs1GWjdNYaP4lf+u+bLHSQGcgCKIZzJMNQ2G5iuentfW+f5kLvvt7C4pQjib7Ny09RTN+tNFAB3I9XhU+Ad1NfUwNWRoDWWxSSHB0LFzXLZwIFNFWqB45h0N/0bvIAVwIrvBdeP+ntocoSw7ImEle0BT2+Fyu2KmZBsCBpAY704bTre164QQrp3gzYgacEbaQqY9aLQUz747dLzSIJHJIDlFAy4DL5FIJBKJRCKRSCQSiUQikUgkEolEIpFIJBKJRCKRSCQSiUQikUgkEolEIpFIJBKJRCKRSCQSiUQiOXH5fyFTFhRgM9B/AAAAAElFTkSuQmCC"


# ═══════════════════════════════════════════════════════════════════
# CSS — themed via custom properties so a future light-mode toggle is
# a one-class swap on <html>. All component classes live here; new
# pages should compose existing classes rather than inline styles.
# ═══════════════════════════════════════════════════════════════════
# _BASE_CSS now lives in app/static/app.css, served via the /static
# route — real .css avoids the Python-string-literal fragility that
# broke the UI in v1.22.0 (#40) and lets editors lint it.


# Vanilla JS shipped on every page — provides:
#   - Tabs (data-tabs / data-tab-target)
#   - Toasts (window.dsToast(msg, kind))
#   - Cross-page toast carryover via localStorage
#   - Confirm dialog (window.dsConfirm(message, onYes))
# _BASE_JS now lives in app/static/app.js, served via the /static route
# (same rationale as app.css above).


# Cloud metadata endpoints — credential theft targets, hard-blocked
_CLOUD_METADATA_HOSTS = {
    "169.254.169.254",          # AWS, Azure, OpenStack, DigitalOcean (IPv4 link-local)
    "fd00:ec2::254",            # AWS IPv6 metadata
    "metadata.google.internal", # GCP
    "metadata.goog",            # GCP
    "metadata",                 # Some cloud providers' short hostname
}

# Discord webhook hosts (used for stricter discord_webhook validation)
_DISCORD_HOSTS = {
    "discord.com",
    "discordapp.com",
    "canary.discord.com",
    "ptb.discord.com",
}


def _validate_cron(expr):
    """Validate a 5-field cron expression. Returns (ok, error_message).

    Mirrors the parsing logic in Scheduler._matches_cron — if any field would
    raise ValueError there at runtime, validation fails here at save time.
    Empty / blank expressions are rejected (cron is required).
    """
    if not expr or not expr.strip():
        return False, "empty"
    parts = expr.strip().split()
    if len(parts) != 5:
        return False, f"need 5 space-separated fields, got {len(parts)}"

    field_names = ("minute", "hour", "day-of-month", "month", "day-of-week")
    for name, pattern in zip(field_names, parts, strict=True):
        if pattern == "*":
            continue
        try:
            if "/" in pattern and "-" in pattern.split("/")[0]:
                range_part, step_part = pattern.split("/", 1)
                start_s, end_s = range_part.split("-")
                int(start_s); int(end_s); int(step_part)
            elif pattern.startswith("*/"):
                int(pattern[2:])
            elif "," in pattern:
                for v in pattern.split(","):
                    int(v)
            elif "-" in pattern:
                start_s, end_s = pattern.split("-")
                int(start_s); int(end_s)
            else:
                int(pattern)
        except (ValueError, IndexError):
            return False, f"invalid {name} field: {pattern!r}"

    return True, None


def _validate_webhook_url(url, kind="generic"):
    """
    Validate a user-supplied webhook URL.

    kind="generic": http(s) only, blocks cloud metadata endpoints. Allows
        private/LAN addresses (selfhosted users frequently target Ntfy/Gotify/
        Home Assistant on internal networks — that's legitimate).
    kind="discord": additionally requires the host to be an official Discord
        webhook host.

    Returns (ok: bool, error_message: str|None). Empty/blank URLs are treated
    as "disabled" and pass validation.
    """
    if not url or not url.strip():
        return True, None

    url = url.strip()

    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"Invalid URL ({exc})"

    if parsed.scheme.lower() not in ("http", "https"):
        return False, f"Only http:// and https:// URLs are allowed (got {parsed.scheme!r})"

    if not parsed.hostname:
        return False, "URL has no hostname"

    host_lower = parsed.hostname.lower()

    # Cloud metadata: block hostname form
    if host_lower in _CLOUD_METADATA_HOSTS:
        return False, f"Cloud metadata endpoint ({host_lower}) is blocked"

    # Cloud metadata: block IP-literal form (e.g. http://169.254.169.254)
    try:
        ip = ipaddress.ip_address(host_lower)
        if str(ip) in _CLOUD_METADATA_HOSTS:
            return False, "Cloud metadata endpoint IP is blocked"
        # Also block link-local addresses — they're rarely used legitimately
        # and can be abused (AWS metadata is a link-local IP).
        if ip.is_link_local:
            return False, f"Link-local address ({ip}) is blocked"
    except ValueError:
        pass  # Not a literal IP, that's fine

    if kind == "discord" and host_lower not in _DISCORD_HOSTS:
        return False, f"Discord webhook host must be discord.com (got {host_lower})"

    return True, None


class _StoreScope:
    """The two attributes `update_engine.host_store` reads to decide which
    view of the container state a host gets (#7).

    `host_store` is deliberately a free function taking an "owner" rather
    than a method, so the update orchestration can run with several kinds
    of `self`. The Web UI has no such object — the handler is a class, not
    an instance, at the point the store has to be resolved — so it brings
    the smallest possible one.
    """

    def __init__(self, store, hosts):
        self.store = store
        self.hosts = hosts


def create_handler(config, checker, bot, store, password=None, backend=None,
                   hosts=None):
    """Create a request handler with access to app components."""

    # Container CLI seam (v2 groundwork). Resolved here once so the read
    # views can go through `backend`; defaulting keeps existing callers
    # (and render tests that build the handler directly) working unchanged.
    if backend is None:
        from container_backend import get_backend
        backend = get_backend(config)

    # Multi-host registry (#7). The status table lists every managed host
    # and every action runs against the host its row belongs to, resolved
    # through the registry by `_resolve_host` below. `hosts is None`
    # (render tests, embedders) and the single-host case are the same thing
    # here: nothing extra renders and nothing extra resolves.
    def _multi_hosts():
        """The registry when it actually manages more than the local host.

        Returns None otherwise, so every caller can gate on one truthy
        check and single-host output stays byte-for-byte what it was.
        """
        try:
            return hosts if (hosts is not None and hosts.is_multi) else None
        except Exception:
            return None

    def _resolve_host(name):
        """The `(name, backend, checker, store)` an action must act through.

        `name` comes out of `container_store.split_host_key` on the `name`
        field of a request — deliberately the SAME key shape the Telegram
        callbacks use (`nginx` local, `nas/nginx` remote), so there is one
        identifier format in the project and not two. A bare name splits to
        the local host, which is what every bookmarked POST, every
        single-host form and every hand-rolled client sends.

        Two rules do the actual safety work here:

          * a request that names no host is LOCAL. Never "guess from the
            container name" — two boxes may both run an `nginx` and the
            page that offered the button knows perfectly well which one it
            meant.
          * a request naming a host this instance does not manage resolves
            to **None**, and the caller must then do nothing at all. A
            refused action is a click the user repeats; an action applied
            to the wrong machine is not recoverable.

        The local host always gets back the very objects `create_handler`
        was constructed with, so no single-host code path is even reachable
        by the registry.
        """
        from container_store import LOCAL_HOST
        name = (name or "").strip().lower()
        if not name or name == LOCAL_HOST:
            return LOCAL_HOST, backend, checker, _store_for(LOCAL_HOST)
        multi = _multi_hosts()
        if multi is None:
            return None
        host = multi.get(name)
        if host is None:
            return None
        if host.is_local:
            return LOCAL_HOST, backend, checker, _store_for(LOCAL_HOST)
        return host.name, host.backend, host.checker, host.store

    #: The `.store` / `.hosts` pair `update_engine.host_store` resolves
    #: against — built once, since neither ever changes for a handler.
    _scope = _StoreScope(store, hosts)

    def _store_for(host_name):
        """Container state scoped to one host — see `update_engine.host_store`
        for why a single-host install deliberately gets the RAW store back
        rather than a `HostScopedStore(store, "local")`."""
        from update_engine import host_store
        return host_store(_scope, host_name)

    # The one host every request means when it names none. Bound here so
    # the comparisons below read as prose rather than as a string literal.
    from container_store import LOCAL_HOST as _LOCAL_HOST

    class WebHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Suppress default logging

        def _check_auth(self):
            """Check Basic Auth against the currently configured password.

            Reads `config.web_password` fresh on every request rather than
            a hash cached at startup, so a password changed in Settings ›
            General takes effect immediately — no restart. (The `password`
            argument to create_handler is just the startup value of the
            same field; config is the source of truth.) One SHA-256 per
            request is nothing. Uses hmac.compare_digest to avoid the
            timing side-channel of `==` on the hashes.
            """
            # getattr, not config.web_password directly: the real Config
            # always carries it (a constructor arg), so this is a no-op in
            # production — but it keeps auth from 500-ing a request on a
            # config that somehow lacks the attribute, degrading to the
            # documented "no password set → open" instead of crashing.
            current = getattr(config, "web_password", "") or ""
            if not current:
                return True
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                user, pw = decoded.split(":", 1)
                submitted = hashlib.sha256(pw.encode()).hexdigest()
                expected = hashlib.sha256(current.encode()).hexdigest()
                return hmac.compare_digest(submitted, expected)
            except Exception:
                return False

        def _check_csrf(self):
            """Origin/Referer-based CSRF check for state-changing requests.

            Modern browsers always send `Origin` on cross-origin POSTs (and
            usually on same-origin POSTs too). For older browsers we fall
            back to `Referer`. Either header's host:port must match the
            request's `Host` header.

            A request that arrives without Origin AND without Referer is
            rejected — every legitimate browser sends at least one.
            """
            host = (self.headers.get("Host") or "").strip().lower()
            if not host:
                return False

            origin = (self.headers.get("Origin") or "").strip()
            referer = (self.headers.get("Referer") or "").strip()
            source = origin or referer
            if not source:
                return False

            try:
                source_netloc = urlparse(source).netloc.lower()
            except Exception:
                return False

            if not source_netloc:
                return False

            # Compare host:port. Browsers always include the port in netloc
            # when it's non-default, and the Host header includes it too.
            return source_netloc == host

        def _send_auth_required(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Docksentry"')
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h1>401 - Login required</h1>")

        def _send_forbidden(self, reason="Forbidden"):
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<h1>403 - {_e(reason)}</h1>".encode())

        def _send_html(self, html, status=200):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

        def _send_redirect(self, path="/"):
            self.send_response(303)
            self.send_header("Location", path)
            self.end_headers()

        def _get_path(self):
            """Return path without query string."""
            return urlparse(self.path).path

        def _get_containers(self):
            """Every running container on the LOCAL host.

            Kept as a zero-argument method: it is the seam the render tests
            replace, and `_containers_on` below is the same code with the
            backend as a parameter so a remote host's rows can be built from
            exactly the same data (health, labels, OCI version, image id) as
            the local ones.
            """
            return self._containers_on(backend)

        def _containers_on(self, be, timeout=None):
            # Use docker inspect (not docker ps Status-string parsing) so health
            # detection works on both Docker and Podman. Podman's REST API does
            # not append `(healthy)` to the Status field — that's a Docker CLI
            # cosmetic — but State.Health.Status is consistently provided by
            # both. Reported by LeeNX in #28 for podman-compose containers.
            #
            # `timeout=None` is what every call passed before this took a
            # parameter, and the backend then applies its own default — so
            # the local path issues byte-identical argv with identical
            # semantics. Remote callers pass a short one: an endpoint that
            # answers slowly must not hold the status page.
            ids_p = be.ps(quiet=True, timeout=timeout)
            ids = [i for i in ids_p.stdout.strip().split("\n") if i]
            if not ids:
                return []
            ins_p = be.inspect(ids, timeout=timeout)
            try:
                inspected = json.loads(ins_p.stdout) or []
            except (json.JSONDecodeError, ValueError):
                return []

            # Batch image-inspect to read OCI version labels + short image
            # IDs. Requested by @LeeNX in #32 — knowing whether a container
            # is on v30.0.1 vs v30.0.2 of an upstream image is impossible
            # from the bare tag (often `latest`). Pulling
            # `org.opencontainers.image.version` is the cheap honest answer
            # when the upstream image sets it (~40% coverage in the wild).
            # Inspect by the container's RUNNING image ID (top-level
            # .Image), not the tag: a tag can move to a newer image while
            # the container still runs the old one — keying by tag showed
            # the tag's version, not the container's (#46, @LeeNX saw
            # "new version in the table, old version everywhere else").
            unique_images = list({
                c.get("Image", "") for c in inspected if c.get("Image")
            })
            image_info = {}  # image_ref -> {"version": "...", "short_id": "abcd1234"}
            if unique_images:
                img_p = be.image_inspect(unique_images, timeout=timeout)
                try:
                    img_data = json.loads(img_p.stdout) or []
                except (json.JSONDecodeError, ValueError):
                    img_data = []
                # Build a lookup by every name that maps to each image.
                for entry in img_data:
                    labels = (entry.get("Config") or {}).get("Labels") or {}
                    version = (labels.get("org.opencontainers.image.version") or "").strip()
                    image_id = entry.get("Id", "")
                    short_id = image_id[7:19] if image_id.startswith("sha256:") else image_id[:12]
                    info = {"version": version, "short_id": short_id}
                    image_info[image_id] = info
                    for tag in (entry.get("RepoTags") or []):
                        image_info[tag] = info
                    for digest in (entry.get("RepoDigests") or []):
                        image_info[digest] = info

            containers = []
            for cfg in inspected:
                name = (cfg.get("Name") or "?").lstrip("/")
                image = (cfg.get("Config") or {}).get("Image", "?")
                state = cfg.get("State") or {}
                health = (state.get("Health") or {}).get("Status", "")
                # Synthesize a Docker-compatible status string so the rest of
                # the rendering pipeline (which greps for "healthy"/"starting")
                # keeps working unchanged.
                base = "Up"
                if health == "healthy":
                    status_str = f"{base} (healthy)"
                elif health == "unhealthy":
                    status_str = f"{base} (unhealthy)"
                elif health == "starting":
                    status_str = f"{base} (health: starting)"
                else:
                    status_str = base
                # Version / hash from image_info lookup (may be empty if
                # the image doesn't carry the OCI label).
                # Running image ID first — the tag may already point at a
                # newer image than the one this container actually runs.
                info = image_info.get(cfg.get("Image", "")) or image_info.get(image) or {}
                containers.append({
                    "name": name,
                    "image": image,
                    "status": status_str,
                    "health": health,
                    "version": info.get("version", ""),
                    "short_id": info.get("short_id", ""),
                    # docksentry.* labels drive per-container overrides
                    # (#42) — carried along so the table can show EFFECTIVE
                    # states instead of just the stored toggles.
                    "labels": (cfg.get("Config") or {}).get("Labels") or {},
                })
            return containers

        def _is_own_container(self, name):
            """True when `name` is the Docksentry container we run in.

            Never true when self-detection comes back empty (QNAP / Podman
            corner cases, scripts/test_self_detection.py) — an unresolved
            own name must leave every code path exactly as it was.
            """
            if not name:
                return False
            try:
                own = checker._own_container_name()
            except Exception:
                return False
            return bool(own) and name == own

        # ── Repo / changelog links (#52, @LeeNX) ──────────────────────
        # Three helpers so every place that renders a link goes through
        # the same gate. Nothing here ever builds an `href` without a
        # fresh `is_safe_link()` — see `_link_anchor`.

        def _row_link(self, c, links):
            """(url, kind) for one status-table row — WITHOUT a single
            extra `docker inspect`.

            Same priority chain as `LinkResolver.resolve_link_with_kind`
            (label → stored → OCI source → OCI url → registry guess),
            but fed from data `_get_containers` already has in hand. That
            matters: the table renders every running container, so one
            inspect per row would turn the status page into an N-call
            fan-out on hosts with 50+ containers.

            The chain is free here because `c["labels"]` comes from
            `docker inspect <ids>` → `.Config.Labels`, which is the exact
            same dict `UpdateChecker.get_container_labels()` returns —
            and Docker merges the *image's* labels into a container's
            Config.Labels, so `org.opencontainers.image.source` / `.url`
            are already present (verified against a live daemon: 7 of 19
            running containers carry OCI labels there). The registry
            fallback is pure string work on the image reference, no
            network, no daemon.

            `kind` is one of "label" | "manual" | "source" | "url" |
            "registry" | "none" — same vocabulary as the bot, so the
            origin wording is shared between both surfaces.
            """
            from container_store import is_safe_link
            labels = c.get("labels") or {}

            def _lab(key):
                v = labels.get(key)
                return str(v).strip() if isinstance(v, str) else ""

            # 0. `docksentry.link` container label — GitOps source of
            #    truth. No `.lower()`: URL paths are case-sensitive.
            raw = _lab("docksentry.link")
            if raw and is_safe_link(raw):
                return raw, "label"
            # 1. Manual override stored via Web UI / `/setlink`.
            stored = links.get(c.get("name", ""))
            stored = str(stored).strip() if isinstance(stored, str) else ""
            if stored and is_safe_link(stored):
                return stored, "manual"
            # 2 + 3. OCI labels off the image.
            for key, kind in (("org.opencontainers.image.source", "source"),
                              ("org.opencontainers.image.url", "url")):
                v = _lab(key)
                if v and is_safe_link(v):
                    return v, kind
            # 4. Registry-overview heuristic. `guess_registry_overview_url`
            #    is a pure string mapping on LinkResolver; reused rather
            #    than copied so bot and Web UI can never drift apart.
            #    `bot` is None in headless/test setups — then we deliberately
            #    keep suppressing the guess (unchanged behaviour), which is
            #    a fine answer.
            image = c.get("image") or ""
            if image and image != "?" and bot is not None:
                try:
                    guess = LinkResolver.guess_registry_overview_url(image)
                except Exception:
                    guess = ""
                if guess and is_safe_link(guess):
                    return guess, "registry"
            return "", "none"

        def _link_origin_text(self, t, kind):
            """Human wording for where a link came from. The user
            otherwise cannot tell a changelog URL they typed themselves
            from one Docksentry guessed off the image name — and the
            registry guess is frequently *not* a changelog."""
            return {
                "label": t("web_link_origin_label"),
                "manual": t("web_link_origin_manual"),
                "source": t("web_link_origin_source"),
                "url": t("web_link_origin_url"),
                "registry": t("web_link_origin_registry"),
            }.get(kind, "")

        def _link_anchor(self, t, url, kind, text="🔗", attrs=""):
            """A single `<a>` for a container link, or "" when there is
            nothing safe to render.

            THE choke point for defence in depth: `is_safe_link` runs
            again right here, on the way into the `href`. Values in
            `container_links.json` predating the validation (restored
            from an old backup, hand-edited, written by a pre-#52 build)
            were never checked on the way in, and `html.escape()` does
            not touch a URL scheme — `javascript:alert(1)` survives
            escaping intact and fires on click. No fresh check, no link.

            `target="_blank"` always ships with `rel="noopener
            noreferrer"`, same as the four footer links: without
            `noopener` the opened page gets a `window.opener` handle
            back into the Docksentry UI.
            """
            from container_store import is_safe_link
            if not url or not is_safe_link(url):
                return ""
            origin = self._link_origin_text(t, kind)
            title = t("web_link_open_tt")
            if origin:
                title = f"{title} — {origin}"
            pre = f" {attrs}" if attrs else ""
            return (f'<a{pre} href="{_e(url)}" target="_blank" '
                    f'rel="noopener noreferrer" title="{_e(title)}">{text}</a>')

        def _action_target(self, params, field="name"):
            """Who an action request is about: `(host, name, backend,
            checker, store)`, or **None** when it must not run (#7).

            The `name` field of every action POST is a HOST KEY — `nginx`
            for the local host, `nas/nginx` for a remote one — which is the
            same identifier `container_store.host_key` writes into the
            state files and the same one the Telegram callbacks carry. One
            format, three surfaces.

            Two ways to get None, and both mean "do nothing":

              * an empty name, as before;
              * a host this instance does not manage — a form from before
                a `DOCKER_HOSTS` edit, or a hand-rolled POST. Falling back
                to the local host there is precisely the wrong-host bug
                this whole feature has to not have.

            A name with no host in it is the LOCAL host, always. That is
            what every bookmarked POST, every single-host form and every
            older client sends, so they all keep working untouched.
            """
            from container_store import split_host_key
            raw = (params.get(field, [""])[0] or "").strip()
            if not raw:
                return None
            host_name, name = split_host_key(raw)
            if not name:
                return None
            resolved = _resolve_host(host_name)
            if resolved is None:
                return None
            hname, hbackend, hchecker, hstore = resolved
            return hname, name, hbackend, hchecker, hstore

        def _back_to_container(self, target):
            """Where a per-container form returns to after it saved.

            The detail view answers for the local host, so a remote target
            goes back to the status table instead of to a URL that would
            describe a different machine's container of the same name.
            `None` — nothing was done — also lands on the table, which is
            what the old `if name else "/"` did.
            """
            if target is None:
                return "/"
            return (f"/container/{target[1]}" if target[0] == _LOCAL_HOST
                    else "/")

        def _get_pending(self, host=None):
            """Pending updates for ONE host — the local one by default.

            The file holds every managed host's entries (#7). The Web UI
            acts exclusively on the machine Docksentry runs on, so handing
            it the whole file made a *local* row light up with an Update
            button because a *remote* host had an update for a container of
            the same name — and clicking it recreated the local container
            from the remote entry's image. Filtering here fixes the badge,
            the detail-page button and the update action in one place.
            """
            from container_store import LOCAL_HOST, entry_host
            if not os.path.exists(config.pending_file):
                return []
            try:
                with open(config.pending_file) as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                return []
            if not isinstance(data, list):
                return []
            wanted = host or LOCAL_HOST
            return [u for u in data
                    if isinstance(u, dict) and entry_host(u) == wanted]

        def _render_page(self, content, active="status"):
            from version import VERSION
            from maintenance import get_state as _maint_state, format_remaining as _maint_remaining
            t = _web_translator(config.language)

            nav_items = [
                ("status", f'📊 {t("web_nav_status")}', "/"),
                ("groups", f'📦 {t("web_nav_groups")}', "/groups"),
                ("history", f'📋 {t("web_nav_history")}', "/history"),
                ("logs", f'📜 {t("web_nav_logs")}', "/logs"),
                ("settings", f'⚙️ {t("web_nav_settings")}', "/settings"),
            ]
            nav_html = ""
            for key, label, href in nav_items:
                cls = ' class="active"' if key == active else ""
                nav_html += f'<a href="{href}"{cls}>{label}</a> '

            # Maintenance banner (visible on every page when active)
            mstate = _maint_state(config)
            maint_banner = ""
            if mstate.get("active"):
                if mstate.get("until_iso") == "forever":
                    until_text = t("web_maint_forever")
                else:
                    remaining = _maint_remaining(mstate)
                    until_text = t("web_maint_until", remaining=remaining)
                maint_banner = f"""<div class="maint-banner">
<span class="maint-banner-icon">🛠</span>
<span><strong>{t("web_maint_active")}</strong> — {until_text}</span>
<form method="POST" action="/api/maintenance" style="margin-left:auto">
<input type="hidden" name="action" value="off">
<button type="submit" class="btn-sm btn-outline">{t("web_maint_disable")}</button>
</form>
</div>"""

            # Simple/Advanced UI mode — toggles a body class that hides
            # `.adv-only` elements via CSS.
            ui_mode = getattr(config, "ui_mode", "advanced")
            if ui_mode not in ("simple", "advanced"):
                ui_mode = "advanced"
            body_class = "mode-simple" if ui_mode == "simple" else "mode-advanced"
            ui_mode_other = "advanced" if ui_mode == "simple" else "simple"
            if ui_mode == "simple":
                ui_mode_toggle_title = t("web_ui_mode_show_advanced")
                # Wrench icon — switch to advanced
                ui_mode_icon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>'
            else:
                ui_mode_toggle_title = t("web_ui_mode_show_simple")
                # User icon — switch to simple
                ui_mode_icon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>'

            return f"""<!DOCTYPE html>
<html lang="{_e(config.language)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Docksentry</title>
<link rel="manifest" href="/static/manifest.webmanifest?v={VERSION}">
<meta name="theme-color" content="#161b22">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Docksentry">
<link rel="apple-touch-icon" href="/static/icon.svg">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='6' fill='%23161b22'/><path d='M16 5 L26 9 V17 C26 22 21 26 16 28 C11 26 6 22 6 17 V9 Z' fill='%2358a6ff' stroke='%23ffffff' stroke-width='1.2'/><circle cx='16' cy='17' r='4.5' fill='%23161b22' stroke='%23ffffff' stroke-width='1.6'/><circle cx='16' cy='17' r='1.8' fill='%23ffffff'/></svg>">
<script>
// Apply theme before paint to avoid flash. Reads localStorage; falls back
// to OS preference (prefers-color-scheme) when nothing is stored.
(function() {{
    try {{
        var saved = localStorage.getItem('ds-theme');
        var theme = saved || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
        if (theme === 'light') document.documentElement.setAttribute('data-theme', 'light');
    }} catch(e) {{}}
}})();
</script>
<link rel="stylesheet" href="/static/app.css?v={VERSION}">
</head>
<body class="{body_class}">
<div class="header">
<div class="header-row">
<div class="header-brand">
<img src="data:image/png;base64,{_LOGO_B64}" alt="Docksentry">
<h1>Docksentry</h1>
</div>
<div class="header-host-slot"><!-- v2.0: host selector slot --></div>
<!-- .header-form (app.css): inline-flex, not inline — an inline form
     participates in baseline layout and sat a few px lower than its
     flex-child sibling (the theme button). And margin-top:0, which kills
     the remaining 4px offset coming from the global 8px form margin-top
     — both halves of the misalignment @LeeNX screenshotted in #46. -->
<form method="POST" action="/api/ui_mode" class="header-form">
<input type="hidden" name="mode" value="{ui_mode_other}">
<button type="submit" class="btn-icon" title="{ui_mode_toggle_title}">{ui_mode_icon}</button>
</form>
<button type="button" id="ds-theme-toggle" class="btn-icon" title="Toggle theme">
<svg id="ds-theme-icon-dark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:none"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>
<svg id="ds-theme-icon-light" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
</button>
</div>
<div class="nav-wrap"><nav>{nav_html}</nav></div>
</div>
{maint_banner}<div class="content">
{content}
</div>
<div class="footer">
<a href="https://github.com/amayer1983/docksentry/releases/tag/v{VERSION}" target="_blank" rel="noopener noreferrer">Docksentry v{VERSION}</a>
 · <a href="https://github.com/amayer1983/docksentry" target="_blank" rel="noopener noreferrer">GitHub</a>
 · <a href="https://github.com/amayer1983/docksentry/releases" target="_blank" rel="noopener noreferrer">Releases</a>
 · <a href="https://github.com/sponsors/amayer1983" target="_blank" rel="noopener noreferrer">❤ Sponsor</a>
</div>
<script src="/static/app.js?v={VERSION}"></script>
</body>
</html>"""

        def _serve_static(self, name):
            """Serve a cached file from app/static/ with the right Content-Type
            and a 1-day cache (URL carries ?v={VERSION} for cache-busting)."""
            data = _read_static(name)
            if not data:
                self.send_error(404)
                return
            ext = os.path.splitext(name)[1]
            self.send_response(200)
            self.send_header("Content-Type",
                             _STATIC_TYPES.get(ext, "application/octet-stream"))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._check_auth():
                return self._send_auth_required()
            path = self._get_path()
            # Static assets (CSS/JS extracted from Python literals → real
            # files). Served before the setup gate so the wizard is styled.
            if path in ("/static/app.css", "/static/app.js",
                        "/static/manifest.webmanifest", "/static/icon.svg"):
                return self._serve_static(path.rsplit("/", 1)[1])
            # First-run gate: redirect everywhere to /setup until done.
            # /setup itself + /api/* must remain reachable (otherwise the
            # wizard couldn't submit, and the user couldn't escape).
            if (not getattr(config, "web_setup_done", False)
                    and path != "/setup"
                    and not path.startswith("/api/")):
                self._send_redirect("/setup")
                return

            if path == "/" or path == "/status":
                self._page_status()
            elif path == "/history":
                self._page_history()
            elif path == "/groups":
                self._page_groups()
            elif path == "/logs":
                self._page_logs()
            elif path == "/settings":
                self._page_settings()
            elif path == "/setup":
                self._page_setup()
            elif path == "/api/groups_detect":
                # Auto-detect stacks from container labels (v1.21.1).
                # Sources we recognise (in priority order, first match wins):
                #   1. com.docker.compose.project — Compose / Portainer /
                #      Dockge / podman-compose / anything wrapping Compose
                #   2. com.docker.stack.namespace — Docker Swarm stacks
                # Returns JSON: {"ok": true, "stacks": [{"name": ..., "source": ...,
                #   "containers": [{"name": ..., "service": ..., "shares_netns_with": ...}],
                #   "conflicts": {"<container>": "<existing-group-name>"}, "exists": bool}]}
                # Conservative — restart_dependents stays user-choice in the modal.
                try:
                    r = backend.ps(
                        all=True,
                        fmt="{{.Names}}|{{.Image}}|{{.Label \"com.docker.compose.project\"}}|"
                            "{{.Label \"com.docker.compose.service\"}}|"
                            "{{.Label \"com.docker.stack.namespace\"}}",
                        timeout=20,
                    )
                    lines = [l for l in r.stdout.strip().split("\n") if l]
                except subprocess.SubprocessError:
                    lines = []

                # Also need NetworkMode per container to detect VPN-sidecar
                # hints (container:<head>) — informs the modal's default
                # head ordering.
                netns_hints = {}  # container_name → head_name (if shares netns)
                if lines:
                    try:
                        names_only = [l.split("|", 1)[0] for l in lines]
                        ins = backend.inspect(
                            names_only,
                            fmt="{{.Name}}|{{.HostConfig.NetworkMode}}",
                            timeout=20,
                        )
                        for ln in ins.stdout.strip().split("\n"):
                            if not ln or "|" not in ln:
                                continue
                            nm, mode = ln.split("|", 1)
                            nm = nm.lstrip("/")
                            if mode.startswith("container:"):
                                # Resolve container:<id> → name via second inspect
                                # — but that's an extra call per VPN-sidecar.
                                # Cheap shortcut: most users name the sidecar's
                                # head with a recognisable prefix; we just
                                # surface the mode and let the modal handle it.
                                netns_hints[nm] = mode
                    except subprocess.SubprocessError:
                        pass

                # Group by stack name
                by_stack = {}  # stack_name → {"source": "compose"|"swarm", "containers": [...]}
                for line in lines:
                    parts = line.split("|", 4)
                    if len(parts) < 5:
                        continue
                    cname, image, compose_proj, compose_svc, swarm_ns = [p.strip() for p in parts]
                    stack_name = compose_proj or swarm_ns
                    if not stack_name:
                        continue  # No stack label — skip
                    source = "compose" if compose_proj else "swarm"
                    entry = by_stack.setdefault(stack_name, {
                        "source": source, "containers": []
                    })
                    entry["containers"].append({
                        "name": cname,
                        "image": image,
                        "service": compose_svc or "",
                        "netns_hint": netns_hints.get(cname, ""),
                    })

                # Annotate with conflicts (containers already in a Docksentry
                # group) and existing-stack flag (same-named group already
                # exists, so this stack was probably already imported).
                existing_groups = store.get_groups()
                container_to_group = {}  # cname → group_name
                for gid, g in existing_groups.items():
                    for cn in (g.get("containers") or []):
                        container_to_group[cn] = g.get("name", gid)

                stacks = []
                for stack_name, data in sorted(by_stack.items()):
                    members = data["containers"]
                    # Default head order: alphabetical by service (then name).
                    # VPN-sidecar heuristic: if any member has
                    # netns_hint=container:* the OWNER (= head) should be
                    # first. Without resolving the container: target by id
                    # we can't reliably name the head, so we leave ordering
                    # to the user in the modal.
                    members.sort(key=lambda m: (m["service"] or m["name"]))
                    conflicts = {}
                    for m in members:
                        existing = container_to_group.get(m["name"])
                        if existing and existing != stack_name:
                            conflicts[m["name"]] = existing
                    # "exists" = a Docksentry group with the same display
                    # name already exists. UI shows a "skip — already
                    # imported" badge in that case.
                    exists = any(
                        (g.get("name") or "").strip().lower() == stack_name.lower()
                        for g in existing_groups.values()
                    )
                    stacks.append({
                        "name": stack_name,
                        "source": data["source"],
                        "containers": members,
                        "conflicts": conflicts,
                        "exists": exists,
                    })

                payload = json.dumps({"ok": True, "stacks": stacks}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            elif path == "/api/backup_export":
                # Backup all persistent state into one JSON file the user
                # can save to their local machine (v1.22.0, @famewolf in
                # #2 after the config-loss incident). The bundle is
                # restored by /api/backup_import — symmetric format.
                # Keys: schema_version + a per-file dict + a sentinel
                # generated_at + version. We don't include
                # update_history.json (large, regenerates) or
                # pending_updates.json (transient).
                from version import VERSION
                from datetime import datetime as _dt
                bundle = {
                    "schema_version": 1,
                    "generated_at": _dt.now().isoformat(timespec="seconds"),
                    "docksentry_version": VERSION,
                    "settings": {},
                    "pinned": [],
                    "autoupdate": [],
                    "ask_major": [],
                    "groups": {},
                    "notes": {},
                    "links": {},
                    "update_windows": {},
                }
                # Settings — only the user-set keys, not env defaults.
                # save_persistent uses the same key list, so reading the
                # file directly mirrors what we'd save.
                if os.path.exists(config.settings_file):
                    try:
                        with open(config.settings_file) as f:
                            bundle["settings"] = json.load(f)
                    except (IOError, json.JSONDecodeError):
                        pass
                bundle["pinned"] = store.get_pinned()
                bundle["autoupdate"] = store.get_autoupdate()
                bundle["ask_major"] = store.get_ask_before_major()
                bundle["groups"] = store.get_groups()
                bundle["notes"] = store.get_notes()
                bundle["links"] = store.get_links()
                bundle["update_windows"] = store.get_update_windows()
                payload = json.dumps(bundle, indent=2, ensure_ascii=False).encode("utf-8")
                fname = f"docksentry-backup-{_dt.now().strftime('%Y%m%d-%H%M%S')}.json"
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            elif path == "/api/cron_preview":
                # Settings page schedule-editor live preview. Returns
                # the next 3 cron ticks for the `?expr=<cron>` value as
                # short HH:MM strings (today/tomorrow rendered as the
                # weekday name when ≥ 2h away). Pure read — no
                # state changes — and bounded look-ahead.
                from scheduler import cron_next_ticks
                from datetime import datetime as _dt
                query = parse_qs(urlparse(self.path).query)
                expr = (query.get("expr", [""])[0] or "").strip()
                response = {"ok": False, "error": "empty"}
                if expr:
                    parts = expr.split()
                    if len(parts) != 5:
                        response = {"ok": False, "error": "expression needs exactly 5 fields"}
                    else:
                        # Quick validation: try matching now() — if it
                        # raises internally cron_matches returns False;
                        # the next-ticks call below will return empty,
                        # which we surface as a usable error.
                        try:
                            ticks = cron_next_ticks(expr, count=3)
                        except Exception as e:
                            ticks = []
                            response = {"ok": False, "error": str(e)[:120]}
                        else:
                            now = _dt.now()
                            formatted = []
                            for t_dt in ticks:
                                delta = t_dt - now
                                if delta.total_seconds() < 2 * 3600:
                                    # Less than 2 hours away: HH:MM only
                                    formatted.append(t_dt.strftime("%H:%M"))
                                elif t_dt.date() == now.date():
                                    formatted.append("today " + t_dt.strftime("%H:%M"))
                                elif (t_dt.date() - now.date()).days == 1:
                                    formatted.append("tomorrow " + t_dt.strftime("%H:%M"))
                                elif (t_dt.date() - now.date()).days < 7:
                                    formatted.append(t_dt.strftime("%a %H:%M"))
                                else:
                                    formatted.append(t_dt.strftime("%Y-%m-%d %H:%M"))
                            response = {"ok": True, "ticks": formatted}
                payload = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            elif path == "/api/check":
                threading.Thread(target=self._api_check).start()
                self._send_redirect("/")
            elif path == "/api/wizard_skip":
                # Power-user escape hatch: just set the flag and move on.
                config.web_setup_done = True
                config.save_persistent()
                self._send_redirect("/")
            elif path.startswith("/container/"):
                self._page_container(path[len("/container/"):])
            else:
                self._send_html("<h1>404</h1>", 404)

        def do_POST(self):
            if not self._check_auth():
                return self._send_auth_required()
            # CSRF mitigation: every POST must originate from the same host.
            # Forged cross-origin POSTs (from a malicious site abusing the
            # admin's cached Basic Auth credentials) are rejected here.
            if not self._check_csrf():
                return self._send_forbidden("CSRF check failed")
            path = self._get_path()
            if path == "/settings":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)

                # --- Validate before mutating any state ---
                errors = []
                if "cron_schedule" in params and params["cron_schedule"][0].strip():
                    ok, err = _validate_cron(params["cron_schedule"][0].strip())
                    if not ok:
                        errors.append(f"Cron schedule: {err}")
                if "discord_webhook" in params:
                    ok, err = _validate_webhook_url(
                        params["discord_webhook"][0].strip(), kind="discord"
                    )
                    if not ok:
                        errors.append(f"Discord webhook: {err}")
                if "webhook_url" in params:
                    ok, err = _validate_webhook_url(
                        params["webhook_url"][0].strip(), kind="generic"
                    )
                    if not ok:
                        errors.append(f"Webhook URL: {err}")
                if errors:
                    from urllib.parse import quote
                    self._send_redirect("/settings?error=" + quote(" | ".join(errors)))
                    return

                # --- All inputs validated; apply changes ---
                # Update language
                if "language" in params:
                    from i18n import available_languages, get_translator
                    new_lang = params["language"][0]
                    if new_lang in available_languages():
                        config.language = new_lang
                        bot.t = get_translator(new_lang)

                # Update debug & auto_selfupdate / auto_cleanup (checkboxes)
                config.debug = "debug" in params
                config.auto_selfupdate = "auto_selfupdate" in params
                config.auto_cleanup = "auto_cleanup" in params
                config.cleanup_backup_local_only = "cleanup_backup_local_only" in params

                # Numeric cleanup settings — clamp to sane ranges
                if "cleanup_grace_hours" in params:
                    try:
                        v = int(params["cleanup_grace_hours"][0].strip())
                        config.cleanup_grace_hours = max(0, min(v, 8760))  # ≤ 1 year
                    except (ValueError, IndexError):
                        pass
                if "cleanup_backup_days" in params:
                    try:
                        v = int(params["cleanup_backup_days"][0].strip())
                        config.cleanup_backup_days = max(1, min(v, 365))
                    except (ValueError, IndexError):
                        pass

                # Disk-warning settings
                if "disk_warn_percent" in params:
                    try:
                        v = int(params["disk_warn_percent"][0].strip())
                        config.disk_warn_percent = max(50, min(v, 100))
                    except (ValueError, IndexError):
                        pass
                config.disk_warn_auto_cleanup = "disk_warn_auto_cleanup" in params

                # Quiet hours — accept HH:MM or empty
                def _valid_hhmm(s):
                    if not s:
                        return ""
                    try:
                        h, m = s.split(":")
                        if 0 <= int(h) < 24 and 0 <= int(m) < 60:
                            return f"{int(h):02d}:{int(m):02d}"
                    except (ValueError, AttributeError):
                        pass
                    return ""
                if "quiet_hours_start" in params:
                    config.quiet_hours_start = _valid_hhmm(params["quiet_hours_start"][0].strip())
                if "quiet_hours_end" in params:
                    config.quiet_hours_end = _valid_hhmm(params["quiet_hours_end"][0].strip())

                # Weekly report
                config.weekly_report_enabled = "weekly_report_enabled" in params
                if "weekly_report_weekday" in params:
                    try:
                        config.weekly_report_weekday = max(0, min(int(params["weekly_report_weekday"][0]), 6))
                    except (ValueError, IndexError):
                        pass
                if "weekly_report_hour" in params:
                    try:
                        config.weekly_report_hour = max(0, min(int(params["weekly_report_hour"][0]), 23))
                    except (ValueError, IndexError):
                        pass

                # Update cron schedule
                if "cron_schedule" in params and params["cron_schedule"][0].strip():
                    config.cron_schedule = params["cron_schedule"][0].strip()

                # Update exclude containers
                if "exclude_containers" in params:
                    raw = params["exclude_containers"][0].strip()
                    config.exclude_containers = [c.strip() for c in raw.split(",") if c.strip()] if raw else []

                # Update Discord webhook
                if "discord_webhook" in params:
                    config.discord_webhook = params["discord_webhook"][0].strip()

                # Update generic webhook
                if "webhook_url" in params:
                    config.webhook_url = params["webhook_url"][0].strip()

                # Update Telegram Topic ID
                if "telegram_topic_id" in params:
                    config.telegram_topic_id = params["telegram_topic_id"][0].strip()

                # Update Telegram allowed-users whitelist. Empty input
                # clears the list (= "any user in the configured chat").
                if "telegram_allowed_users" in params:
                    raw = params["telegram_allowed_users"][0]
                    config.telegram_allowed_users = [
                        u.strip() for u in raw.split(",") if u.strip()
                    ]

                # Optional BOT_LABEL prefix for multi-bot setups
                if "bot_label" in params:
                    # Cap at 32 chars — Telegram message length isn't a
                    # concern at that size but a runaway label would be
                    # cosmetic noise on every notification.
                    config.bot_label = params["bot_label"][0].strip()[:32]

                # Update / recreate timeouts (Updates tab). Positive
                # seconds; the healthcheck grace has a higher floor than the
                # stop grace but both just need to stay above zero.
                if "healthcheck_max_starting" in params:
                    try:
                        v = int(params["healthcheck_max_starting"][0].strip())
                        config.healthcheck_max_starting = max(30, min(v, 3600))
                    except (ValueError, IndexError):
                        pass
                if "docker_stop_timeout" in params:
                    try:
                        v = int(params["docker_stop_timeout"][0].strip())
                        config.docker_stop_timeout = max(1, min(v, 3600))
                    except (ValueError, IndexError):
                        pass

                # Container-state monitoring (Notifications tab).
                config.monitor_enabled = "monitor_enabled" in params
                if "monitor_interval_seconds" in params:
                    try:
                        v = int(params["monitor_interval_seconds"][0].strip())
                        # Floor of 15s mirrors the Config constructor — the
                        # monitor loop refuses to poll tighter than that.
                        config.monitor_interval_seconds = max(15, min(v, 86400))
                    except (ValueError, IndexError):
                        pass

                # Web UI password change. Only a non-empty submission counts:
                # an empty field means "leave it as it is", never "clear it".
                # Stored verbatim — _check_auth hashes config.web_password
                # fresh on each request and compares, so the plaintext here is
                # exactly what the auth path expects, and the change takes
                # effect on the very next request (no restart). The value is
                # never rendered back into the form or logged (it is not on
                # LOGGABLE_PERSISTENT_KEYS).
                if "web_password" in params:
                    new_pw = params["web_password"][0]
                    if new_pw:
                        config.web_password = new_pw

                # Persist all changes
                config.save_persistent()

                self._send_redirect("/settings?saved=1")
            elif path == "/api/update":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                key = params.get("name", [""])[0].strip()
                if key:
                    threading.Thread(target=self._api_update, args=(key,)).start()
                self._send_redirect("/")
            elif path == "/api/check_one":
                # Per-container update check from the Status table (#50).
                # POST rather than GET so it inherits the auth + CSRF checks
                # above, and answered synchronously: it's a single registry
                # HEAD request, and on a Telegram-less install the JSON
                # response is the *only* feedback the user can get —
                # bot.notify_updates is a no-op without any channel.
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                target = self._action_target(params)
                response = ({"ok": False, "error": "missing name"}
                            if target is None
                            else self._api_check_one(target[1], target[3]))
                payload = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            elif path == "/api/pin":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                target = self._action_target(params)
                if target is not None:
                    # That host's store view, so pinning `nginx` on the NAS
                    # cannot pin the local `nginx` too (#7).
                    target[4].pin(target[1])
                self._send_redirect("/")
            elif path == "/api/unpin":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                target = self._action_target(params)
                if target is not None:
                    target[4].unpin(target[1])
                self._send_redirect("/")
            elif path == "/api/autoupdate":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                target = self._action_target(params)
                # The self-guard is about THIS process, which only ever runs
                # on the local host — a remote box's container that happens
                # to be called `docksentry` is somebody else's instance and
                # its toggle is a perfectly ordinary one.
                if target is not None and not (
                        target[0] == _LOCAL_HOST
                        and self._is_own_container(target[1])):
                    target[4].toggle_auto(target[1])
                # else: our own container. Auto-update for Docksentry is
                # AUTO_SELFUPDATE (Settings › Updates), never the per-container
                # opt-in list — the update flow skips self, and main.py strips
                # us from the list on the next boot anyway. The UI no longer
                # offers the toggle (#51); this is the defence behind it, for
                # bookmarked POSTs and hand-rolled clients.
                self._send_redirect("/")
            elif path == "/api/lifecycle":
                # Container start / stop / restart from the Status page
                # buttons. Same self-kill guard as the Telegram path —
                # refusing to stop/restart ourselves.
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                target = self._action_target(params)
                action = params.get("action", [""])[0]
                if target is not None and action in ("start", "stop", "restart"):
                    _h, name, _be, _ck, _st = target
                    # Self-detection resolves the container THIS process runs
                    # in, which is by definition local — running it against a
                    # remote host would refuse a legitimate action on a
                    # same-named container over there.
                    if (action in ("stop", "restart") and _h == _LOCAL_HOST
                            and checker._would_kill_self(name)):
                        # Silently no-op — the Web UI shouldn't have shown
                        # the button in the first place, but defense in depth.
                        pass
                    else:
                        # Reuse the bot's lifecycle helper for consistent
                        # behaviour (graceful timeout, error reporting). The
                        # checker/backend/host triple has to describe ONE
                        # machine, so all three come from the same target.
                        try:
                            bot._lifecycle_action(action, name, _ck,
                                                  backend=_be, host=_h)
                        except Exception as e:
                            print(f"Lifecycle action failed: {e}")
                ref = self.headers.get("Referer", "/")
                ref_path = urlparse(ref).path or "/"
                self._send_redirect(ref_path)
            elif path == "/api/cleanup":
                threading.Thread(target=self._api_cleanup).start()
                self._send_redirect("/settings?saved=1")
            elif path == "/api/selfupdate":
                threading.Thread(target=self._api_selfupdate).start()
                self._send_redirect("/settings?saved=1")
            elif path == "/api/note":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                target = self._action_target(params)
                note = params.get("note", [""])[0]
                if target is not None:
                    target[4].set_note(target[1], note)
                ref = self.headers.get("Referer", "/")
                ref_path = urlparse(ref).path or "/"
                self._send_redirect(ref_path)
            elif path == "/api/test_webhook":
                # Settings page "Send test" buttons — verify a webhook
                # URL is reachable before saving (#2 feedback). Uses the
                # URL the user just typed (not the saved one) so they
                # can debug a new value without committing it first.
                # Returns JSON for the dsTestWebhook() JS helper.
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                kind = params.get("kind", [""])[0].strip().lower()
                url = params.get("url", [""])[0].strip()

                response = {"ok": False, "error": "unknown"}
                if kind not in ("discord", "webhook"):
                    response = {"ok": False, "error": "invalid kind"}
                elif not url:
                    response = {"ok": False, "error": "empty URL"}
                else:
                    # Build a one-off Notifier with the user-typed URL
                    # injected, leaving the rest of config untouched —
                    # we don't want to mutate the live config or
                    # trigger quiet-hours suppression.
                    from copy import copy as _copy
                    from notifier import Notifier
                    test_config = _copy(config)
                    test_config.discord_webhook = url if kind == "discord" else ""
                    test_config.webhook_url = url if kind == "webhook" else ""
                    # Disable quiet hours for the test so the user
                    # actually sees the result.
                    test_config.quiet_hours_start = ""
                    test_config.quiet_hours_end = ""
                    test_notifier = Notifier(test_config)
                    try:
                        # send_message returns None — surface failures
                        # via stderr-style print; we don't currently
                        # have a structured return. Wrap in try and
                        # consider any exception a failure.
                        test_notifier.send_message(
                            f"🧪 Docksentry test message from Web UI ({kind})"
                        )
                        response = {"ok": True}
                    except Exception as e:
                        response = {"ok": False, "error": str(e)[:200]}

                payload = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            elif path == "/api/link":
                # Per-container repo / changelog URL override (#20, #52).
                # Empty input clears. `set_link` runs the value through
                # the shared `container_store.is_safe_link` — scheme via
                # urlparse, hostname required, no attribute/markdown
                # breakers — and returns False when it refuses.
                #
                # That return value used to be dropped on the floor: the
                # field simply came back empty and the user was left to
                # guess whether the URL had been saved. Now the outcome
                # rides back on the redirect and the container page
                # renders it inline (`link_notice`). This matters more
                # now that the value is actually rendered as an `<a
                # href>` — a silently rejected link looks identical to a
                # link that just doesn't show up.
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                target = self._action_target(params)
                url = params.get("url", [""])[0].strip()
                outcome = ""
                if target is not None:
                    ok = target[4].set_link(target[1], url)
                    if not url:
                        outcome = "cleared"
                    else:
                        outcome = "saved" if ok else "rejected"
                ref = self.headers.get("Referer", "/")
                ref_path = urlparse(ref).path or "/"
                if outcome:
                    ref_path = f"{ref_path}?link={outcome}"
                self._send_redirect(ref_path)
            elif path == "/api/maintenance":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                action = params.get("action", [""])[0].strip().lower()
                from maintenance import enable as _maint_enable, disable as _maint_disable
                if action == "off":
                    _maint_disable(config)
                elif action == "forever":
                    _maint_enable(config, hours=None)
                else:
                    # Hours value: 1, 4, 24, or custom
                    try:
                        hours = float(params.get("hours", ["1"])[0])
                        hours = max(0.0, min(hours, 720.0))  # ≤ 30 days
                        if hours > 0:
                            _maint_enable(config, hours=hours)
                    except (ValueError, IndexError):
                        pass
                # Redirect back where the user came from (relative path only,
                # we don't want to redirect to an external URL even if the
                # Referer says so).
                ref = self.headers.get("Referer", "/")
                ref_path = urlparse(ref).path or "/"
                self._send_redirect(ref_path)
            elif path == "/api/ui_mode":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                mode = params.get("mode", [""])[0].strip().lower()
                if mode in ("simple", "advanced"):
                    config.ui_mode = mode
                    config.save_persistent()
                ref = self.headers.get("Referer", "/")
                ref_path = urlparse(ref).path or "/"
                self._send_redirect(ref_path)
            elif path == "/api/wizard":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)

                # Language
                if "language" in params:
                    from i18n import available_languages, get_translator
                    new_lang = params["language"][0]
                    if new_lang in available_languages():
                        config.language = new_lang
                        if bot.enabled:
                            bot.t = get_translator(new_lang)

                # Cron schedule
                if "cron_schedule" in params and params["cron_schedule"][0].strip():
                    config.cron_schedule = params["cron_schedule"][0].strip()

                # Channels
                if "discord_webhook" in params:
                    cand = params["discord_webhook"][0].strip()
                    ok, _ = _validate_webhook_url(cand, kind="discord")
                    if ok:
                        config.discord_webhook = cand
                if "webhook_url" in params:
                    cand = params["webhook_url"][0].strip()
                    ok, _ = _validate_webhook_url(cand, kind="generic")
                    if ok:
                        config.webhook_url = cand

                # Auto-update mode
                mode = params.get("auto_mode", ["manual"])[0]
                if mode == "all":
                    # Enable auto-update for every running container
                    try:
                        names = [c["name"] for c in self._get_containers()]
                        store.save_autoupdate(names)
                    except Exception:
                        pass
                # "manual" / "picky" → leave the auto list as-is

                # New installs running through the wizard default to the
                # simple UI mode. Existing installs (which never see the
                # wizard because web_setup_done is already true) keep the
                # advanced default.
                config.ui_mode = "simple"
                config.web_setup_done = True
                config.save_persistent()
                self._send_redirect("/?saved=1")
            elif path == "/api/backup_import":
                # Restore a backup bundle created by /api/backup_export.
                # Accepts multipart/form-data with a `file` field. Each
                # known section overwrites the live state via the same
                # save_* methods that handle normal writes (so the
                # atomic-write fix from v1.22.0 applies to the import
                # path too). Unknown / missing sections are silently
                # skipped — forward-compatible if a future schema adds
                # new keys.
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                content_type = self.headers.get("Content-Type", "")
                # Strip multipart boundary to find the JSON payload.
                # Simple parser — we only ever expect one file field.
                body = raw
                if "multipart/form-data" in content_type and b"\r\n\r\n" in raw:
                    # Find the first blank line after the part headers
                    parts = raw.split(b"\r\n\r\n", 1)
                    if len(parts) == 2:
                        # Strip trailing boundary marker
                        body = parts[1]
                        # Last boundary is preceded by --
                        last_boundary = body.rfind(b"\r\n--")
                        if last_boundary > 0:
                            body = body[:last_boundary]
                try:
                    bundle = json.loads(body.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    payload = json.dumps({"ok": False, "error": f"Invalid JSON: {str(e)[:100]}"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                restored = []
                errors = []
                dropped_links = 0   # links rejected by is_safe_link (#52)
                # Settings — apply via the PERSISTENT_KEYS allowlist so
                # we don't accept arbitrary attribute injection.
                if isinstance(bundle.get("settings"), dict):
                    try:
                        for key, value in bundle["settings"].items():
                            if key in PERSISTENT_KEYS:
                                setattr(config, key, value)
                        config.save_persistent()
                        restored.append("settings")
                    except Exception as e:
                        errors.append(f"settings: {str(e)[:100]}")
                # Lists — pinned, autoupdate, ask_major
                if isinstance(bundle.get("pinned"), list):
                    store.save_pinned([str(x) for x in bundle["pinned"] if isinstance(x, str)])
                    restored.append("pinned")
                if isinstance(bundle.get("autoupdate"), list):
                    store.save_autoupdate([str(x) for x in bundle["autoupdate"] if isinstance(x, str)])
                    restored.append("autoupdate")
                if isinstance(bundle.get("ask_major"), list):
                    # No public save_ask_before_major — write through
                    # the same _save the toggle uses. Coerce to str just
                    # to be paranoid about malformed bundles.
                    store._save(store.ask_before_major_file,
                                [str(x) for x in bundle["ask_major"] if isinstance(x, str)])
                    restored.append("ask_major")
                # Dicts — groups, notes, links, update_windows. Just
                # write them through the existing _save_dict so the
                # atomic-write path applies.
                if isinstance(bundle.get("groups"), dict):
                    store._save_dict(store.groups_file, bundle["groups"])
                    restored.append("groups")
                if isinstance(bundle.get("notes"), dict):
                    store._save_dict(store.notes_file, bundle["notes"])
                    restored.append("notes")
                if isinstance(bundle.get("links"), dict):
                    # Links are the one section that gets rendered as an
                    # `<a href>` (#52) — everything else in a bundle ends
                    # up as escaped text. Writing them through _save_dict
                    # raw bypassed set_link and therefore the validator,
                    # so a hand-edited backup file could plant
                    # `javascript:…` in container_links.json and have the
                    # Web UI hand it to the browser on the next render.
                    # A backup is a file, not a trusted channel: it
                    # arrives over an unauthenticated-by-content upload
                    # and nothing about "the user picked it" says the
                    # user wrote it.
                    #
                    # Every entry goes through the same is_safe_link the
                    # live write path uses. Rejects are dropped and
                    # COUNTED — swallowing them silently would restore a
                    # bundle "successfully" while quietly losing data the
                    # user believes is back.
                    from container_store import is_safe_link as _is_safe_link
                    clean_links = {}
                    for k, v in bundle["links"].items():
                        if isinstance(k, str) and isinstance(v, str) and _is_safe_link(v.strip()):
                            clean_links[k] = v.strip()
                        else:
                            dropped_links += 1
                    store._save_dict(store.links_file, clean_links)
                    # The import toast prints `restored` verbatim, so the
                    # count has to travel inside it to be seen at all.
                    restored.append("links" if not dropped_links
                                    else f"links ({dropped_links} unsafe dropped)")
                    if dropped_links:
                        errors.append(
                            f"links: {dropped_links} entry/entries rejected by the "
                            f"URL validator (not http/https, or unsafe characters)")
                if isinstance(bundle.get("update_windows"), dict):
                    store._save_dict(store.update_windows_file, bundle["update_windows"])
                    restored.append("update_windows")
                payload = json.dumps({
                    "ok": True,
                    "restored": restored,
                    "errors": errors,
                    # Machine-readable twin of the note in `restored` —
                    # 0 when the bundle had no links section at all.
                    "links_dropped": dropped_links,
                    "schema_version": bundle.get("schema_version"),
                    "from_version": bundle.get("docksentry_version"),
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            elif path == "/api/group_save":
                # Create OR update a group. When `group_id` is present
                # in the form, the existing group is updated in place
                # (rename, change member list, change flags). When it's
                # absent we generate a slug from `name` for a new group.
                # Source of truth for both flows is store.save_group()
                # which already handles the one-group-per-container
                # invariant. Redirect target is /groups (the dedicated
                # page added in v1.21.0) when the request came from
                # there; the legacy /settings#groups Referer is honoured
                # for backward compatibility with the old layout.
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0].strip()
                containers = params.get("containers", [])
                wait_s = params.get("wait_seconds", ["30"])[0]
                restart_dep = "restart_dependents" in params
                edit_gid = params.get("group_id", [""])[0].strip()
                if name and containers:
                    if edit_gid and edit_gid in store.get_groups():
                        # Update existing group — keep the slug stable.
                        store.save_group(edit_gid, name, containers, wait_s,
                                         restart_dependents=restart_dep)
                    else:
                        # New group — generate a slug.
                        import re as _re
                        slug = _re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "group"
                        existing = store.get_groups()
                        base, n = slug, 2
                        while slug in existing:
                            slug = f"{base}-{n}"
                            n += 1
                        store.save_group(slug, name, containers, wait_s,
                                         restart_dependents=restart_dep)
                ref = self.headers.get("Referer", "")
                target = "/settings#groups" if "/settings" in ref else "/groups"
                self._send_redirect(target)
            elif path == "/api/group_delete":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                gid = params.get("group_id", [""])[0].strip()
                if gid:
                    store.delete_group(gid)
                ref = self.headers.get("Referer", "")
                target = "/settings#groups" if "/settings" in ref else "/groups"
                self._send_redirect(target)
            elif path == "/api/group_reorder":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                gid = params.get("group_id", [""])[0].strip()
                cname = params.get("container", [""])[0].strip()
                direction = params.get("direction", [""])[0].strip()
                if gid and cname and direction in ("up", "down"):
                    store.reorder_group_container(gid, cname, direction)
                ref = self.headers.get("Referer", "")
                target = "/settings#groups" if "/settings" in ref else "/groups"
                self._send_redirect(target)
            elif path == "/api/groups_import_batch":
                # Bulk-create Docksentry groups from auto-detected stacks
                # (v1.21.1). Modal sends multiple `stacks[]=<json>` entries,
                # each carrying {name, containers (ordered), restart_dependents,
                # wait_seconds}. Each gets a fresh slug (or merges into an
                # existing group with the same display name). save_group's
                # one-group-per-container invariant handles re-assignment
                # automatically.
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                raw_stacks = params.get("stacks", [])
                created = 0
                import re as _re
                existing = store.get_groups()
                # Build name → existing_group_id lookup for in-place updates.
                name_to_gid = {
                    (g.get("name") or gid).strip().lower(): gid
                    for gid, g in existing.items()
                }
                for raw in raw_stacks:
                    try:
                        spec = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    sname = (spec.get("name") or "").strip()
                    members = [m for m in (spec.get("containers") or []) if m]
                    if not sname or not members:
                        continue
                    wait_s = int(spec.get("wait_seconds", 30) or 30)
                    rd = bool(spec.get("restart_dependents"))
                    # Same-name → update in place. Different name → fresh slug.
                    existing_gid = name_to_gid.get(sname.lower())
                    if existing_gid:
                        gid = existing_gid
                    else:
                        slug = _re.sub(r"[^a-z0-9-]+", "-", sname.lower()).strip("-") or "stack"
                        base, n = slug, 2
                        while slug in existing:
                            slug = f"{base}-{n}"
                            n += 1
                        gid = slug
                        existing[gid] = {}  # placeholder so the next loop iteration sees it
                    store.save_group(gid, sname, members, wait_s, restart_dependents=rd)
                    created += 1
                payload = json.dumps({"ok": True, "created": created}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            elif path == "/api/group_reorder_batch":
                # Drag-and-drop reorder. Receives the full container
                # order in one POST (`containers[]=a&containers[]=b&...`).
                # Replaces the member list of the named group atomically.
                # Falls back to silently no-op for unknown group ids so
                # the JS handler can stay simple.
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                gid = params.get("group_id", [""])[0].strip()
                new_order = params.get("containers", [])
                groups = store.get_groups()
                g = groups.get(gid)
                if g and new_order:
                    # Preserve members that weren't in the drag payload
                    # (defensive: in case the DOM dropped one). Keep
                    # the rest at their original positions.
                    existing_members = list(g.get("containers") or [])
                    payload_set = set(new_order)
                    # Honour drag order for the payload, then append any
                    # missing ones in their original order.
                    final_order = [c for c in new_order if c in existing_members]
                    for c in existing_members:
                        if c not in payload_set:
                            final_order.append(c)
                    store.save_group(
                        gid,
                        g.get("name", gid),
                        final_order,
                        g.get("wait_seconds", 30),
                        restart_dependents=bool(g.get("restart_dependents")),
                    )
                # Return JSON so the JS fetch can show a toast
                payload = json.dumps({"ok": bool(g)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            elif path == "/api/window":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                target = self._action_target(params)
                name = target[1] if target is not None else ""
                wstore = target[4] if target is not None else store
                action = params.get("action", ["save"])[0]
                if name and action == "delete":
                    wstore.clear_update_window(name)
                elif name and action == "save":
                    start = params.get("start", [""])[0].strip()
                    end = params.get("end", [""])[0].strip()
                    weekdays = [int(d) for d in params.get("weekdays", [])
                                if d.strip().isdigit()]
                    # Basic validation: HH:MM
                    import re as _re
                    if (_re.match(r"^([01][0-9]|2[0-3]):[0-5][0-9]$", start)
                            and _re.match(r"^([01][0-9]|2[0-3]):[0-5][0-9]$", end)):
                        wstore.set_update_window(name, start, end, weekdays)
                self._send_redirect("/settings#windows")
            elif path == "/api/ask_major":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                target = self._action_target(params)
                if target is not None:
                    target[4].toggle_ask_before_major(target[1])
                self._send_redirect("/")
            elif path == "/api/trust_running":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                target = self._action_target(params)
                if target is not None:
                    target[4].toggle_trust_running(target[1])
                self._send_redirect("/")
            elif path == "/api/protect":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                target = self._action_target(params)
                if target is not None:
                    target[4].toggle_protect_stop(target[1])
                self._send_redirect(self._back_to_container(target))
            elif path == "/api/cooldown":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                target = self._action_target(params)
                seconds = params.get("seconds", ["0"])[0].strip()
                if target is not None:
                    target[4].set_cooldown(target[1], seconds)
                self._send_redirect(self._back_to_container(target))
            elif path == "/api/major_confirm":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                raw = params.get("name", [""])[0].strip()
                target = self._action_target(params)
                action = params.get("action", [""])[0]
                if target is not None and action == "confirm":
                    # `_confirm_major_update` takes the host key itself and
                    # resolves the checker for that host — the same call the
                    # Telegram button makes, so both surfaces resume the
                    # update on the machine it was deferred on.
                    threading.Thread(target=bot._confirm_major_update,
                                     args=(checker, raw)).start()
                elif target is not None and action == "reject":
                    target[4].remove_pending_major(target[1])
                self._send_redirect("/")
            elif path == "/api/bulk":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                action = params.get("action", [""])[0]
                keys = params.get("names", [])
                # Form sends a single comma-separated value (from JS join);
                # fall back to multi-value POST if browser sends repeated key.
                # The values are HOST KEYS (bare names for the local host),
                # exactly like the single-container forms.
                if len(keys) == 1 and "," in keys[0]:
                    keys = [n.strip() for n in keys[0].split(",") if n.strip()]
                keys = [n for n in keys if n.strip()]
                if action and keys:
                    threading.Thread(
                        target=self._api_bulk, args=(action, keys)
                    ).start()
                self._send_redirect("/")
            else:
                self._send_html("<h1>404</h1>", 404)

        def _page_setup(self):
            """First-run wizard. Single-page multi-step form, JS-driven.

            Persists `web_setup_done=true` (plus the chosen values) on
            submit, after which the redirect-gate releases the rest of
            the UI.
            """
            from i18n import available_languages
            t = _web_translator(config.language)

            langs = available_languages()
            lang_names = {"en": "English", "de": "Deutsch", "fr": "Français", "es": "Español",
                          "it": "Italiano", "nl": "Nederlands", "pt": "Português", "pl": "Polski",
                          "tr": "Türkçe", "ru": "Русский", "uk": "Українська", "ar": "العربية",
                          "hi": "हिन्दी", "ja": "日本語", "ko": "한국어", "zh": "中文"}
            lang_options = ""
            for l in langs:
                sel = 'selected' if l == config.language else ''
                name = lang_names.get(l, l.upper())
                lang_options += f'<option value="{_e(l)}" {sel}>{_e(name)}</option>\n'

            telegram_on = bool(config.bot_token and config.chat_id)
            telegram_status = ('<span class="badge badge-green">enabled</span>' if telegram_on
                               else '<span class="badge badge-yellow">env-only</span>')
            tele_help = (t("web_setup_telegram_on")
                         if telegram_on else t("web_setup_telegram_off"))

            content = f"""
<div class="card" id="wizard-card" style="max-width:640px;margin:32px auto">

<div class="wizard-head">
<h2 style="margin:0">🚀 {t("web_setup_title")}</h2>
<p class="card-intro" style="margin:6px 0 0 0">{t("web_setup_intro")}</p>
</div>

<div class="wizard-stepper">
<span class="wstep is-active" data-step="1">1</span>
<span class="wstep-bar"></span>
<span class="wstep" data-step="2">2</span>
<span class="wstep-bar"></span>
<span class="wstep" data-step="3">3</span>
<span class="wstep-bar"></span>
<span class="wstep" data-step="4">4</span>
</div>

<form method="POST" action="/api/wizard">

<!-- ── Step 1: Language ──────────────────────────────────── -->
<div class="wstep-pane is-active" data-step-pane="1">
<h3 style="font-size:15px;color:var(--accent);margin-bottom:8px">🌐 {t("web_setup_lang_title")}</h3>
<p class="form-help" style="margin:0 0 12px 0">{t("web_setup_lang_intro")}</p>
<select name="language">{lang_options}</select>
</div>

<!-- ── Step 2: Schedule ──────────────────────────────────── -->
<div class="wstep-pane" data-step-pane="2">
<h3 style="font-size:15px;color:var(--accent);margin-bottom:8px">⏰ {t("web_setup_schedule_title")}</h3>
<p class="form-help" style="margin:0 0 12px 0">{t("web_setup_schedule_intro")}</p>
<div class="wizard-presets">
<button type="button" class="btn btn-outline btn-sm wizard-preset" data-cron="0 18 * * *">{t("web_setup_preset_daily_evening")}</button>
<button type="button" class="btn btn-outline btn-sm wizard-preset" data-cron="0 9 * * 1">{t("web_setup_preset_weekly_mon")}</button>
<button type="button" class="btn btn-outline btn-sm wizard-preset" data-cron="0 * * * *">{t("web_setup_preset_hourly")}</button>
<button type="button" class="btn btn-outline btn-sm wizard-preset" data-cron="0 6 * * *">{t("web_setup_preset_daily_morning")}</button>
</div>
<label style="margin-top:12px">{t("web_setup_cron_label")}</label>
<input type="text" name="cron_schedule" id="wizard-cron" value="{_e(config.cron_schedule)}" placeholder="0 18 * * *">
<p class="form-help">{t("web_setup_cron_help")}</p>
</div>

<!-- ── Step 3: Channels ──────────────────────────────────── -->
<div class="wstep-pane" data-step-pane="3">
<h3 style="font-size:15px;color:var(--accent);margin-bottom:8px">🔔 {t("web_setup_channels_title")}</h3>
<p class="form-help" style="margin:0 0 12px 0">{t("web_setup_channels_intro")}</p>

<div style="background:var(--bg);border:1px solid var(--border-soft);border-radius:6px;padding:10px 12px;margin-bottom:12px">
<strong>Telegram</strong> {telegram_status}
<p class="form-help" style="margin:4px 0 0 0">{tele_help}</p>
</div>

<label>Discord Webhook ({t("web_setup_optional")})</label>
<input type="text" name="discord_webhook" value="{_e(config.discord_webhook)}" placeholder="https://discord.com/api/webhooks/...">

<label>Generic Webhook ({t("web_setup_optional")})</label>
<input type="text" name="webhook_url" value="{_e(config.webhook_url)}" placeholder="https://your-service/webhook">
</div>

<!-- ── Step 4: Auto-update behavior ──────────────────────── -->
<div class="wstep-pane" data-step-pane="4">
<h3 style="font-size:15px;color:var(--accent);margin-bottom:8px">🔄 {t("web_setup_auto_title")}</h3>
<p class="form-help" style="margin:0 0 12px 0">{t("web_setup_auto_intro")}</p>

<div class="wizard-radio">
<label class="wizard-radio-row">
  <input type="radio" name="auto_mode" value="manual" checked>
  <span><strong>{t("web_setup_auto_manual")}</strong><br>
  <span class="form-help" style="margin:0">{t("web_setup_auto_manual_hint")}</span></span>
</label>
<label class="wizard-radio-row">
  <input type="radio" name="auto_mode" value="all">
  <span><strong>{t("web_setup_auto_all")}</strong><br>
  <span class="form-help" style="margin:0">{t("web_setup_auto_all_hint")}</span></span>
</label>
<label class="wizard-radio-row">
  <input type="radio" name="auto_mode" value="picky">
  <span><strong>{t("web_setup_auto_picky")}</strong><br>
  <span class="form-help" style="margin:0">{t("web_setup_auto_picky_hint")}</span></span>
</label>
</div>
</div>

<div class="wizard-nav">
<button type="button" class="btn btn-outline" id="wizard-back" disabled>← {t("web_setup_back")}</button>
<a href="/api/wizard_skip" class="btn-back" style="align-self:center;font-size:13px">{t("web_setup_skip")}</a>
<button type="button" class="btn" id="wizard-next">{t("web_setup_next")} →</button>
<button type="submit" class="btn" id="wizard-finish" style="display:none">✓ {t("web_setup_finish")}</button>
</div>
</form>
</div>

<script>
(function() {{
    const TOTAL = 4;
    let cur = 1;
    const steps = document.querySelectorAll('.wstep');
    const panes = document.querySelectorAll('.wstep-pane');
    const back = document.getElementById('wizard-back');
    const next = document.getElementById('wizard-next');
    const finish = document.getElementById('wizard-finish');

    function render() {{
        steps.forEach(s => {{
            const n = parseInt(s.dataset.step);
            s.classList.toggle('is-active', n === cur);
            s.classList.toggle('is-done',   n <  cur);
        }});
        panes.forEach(p => {{
            p.classList.toggle('is-active', parseInt(p.dataset.stepPane) === cur);
        }});
        back.disabled = (cur === 1);
        next.style.display   = (cur === TOTAL) ? 'none' : '';
        finish.style.display = (cur === TOTAL) ? '' : 'none';
    }}
    back.addEventListener('click', () => {{ if (cur > 1)     {{ cur--; render(); }} }});
    next.addEventListener('click', () => {{ if (cur < TOTAL) {{ cur++; render(); }} }});

    // Cron preset buttons
    const cronInput = document.getElementById('wizard-cron');
    document.querySelectorAll('.wizard-preset').forEach(b => {{
        b.addEventListener('click', () => {{
            cronInput.value = b.dataset.cron;
            document.querySelectorAll('.wizard-preset').forEach(x => x.classList.remove('is-active'));
            b.classList.add('is-active');
        }});
    }});

    render();
}})();
</script>"""
            self._send_html(self._render_page(content, "status"))

        def _status_view(self, host_name, hstore, containers, own_name):
            """Everything ONE host's rows get rendered from, read once.

            A single-host install builds exactly one of these — for the
            local host, out of the very objects this handler was
            constructed with (`_store_for("local")` hands back the raw
            store there) — so every value in it is what the old inline
            code read, in the same shape.

            `own_name` is empty for every remote host on purpose: a box
            elsewhere may well run a container called `docksentry`, but it
            is somebody else's, not the process rendering this page, and
            the self-guards must not fire on it.
            """
            # container_name → (group_id, group_name)
            groups_lookup = {}
            for gid, g in (hstore.get_groups() or {}).items():
                gname = g.get("name", gid)
                for cname in g.get("containers") or []:
                    groups_lookup[cname] = (gid, gname)
            pending = self._get_pending(host_name)
            return {
                "host": host_name,
                "store": hstore,
                "containers": containers,
                "own_name": own_name,
                "pending": pending,
                "pending_names": [u["name"] for u in pending],
                "pinned": hstore.get_pinned(),
                "auto_list": hstore.get_autoupdate(),
                "ask_major": hstore.get_ask_before_major(),
                "groups": groups_lookup,
                "notes": hstore.get_notes(),
                # One read for the whole table (#52) — `_row_link` takes the
                # dict, so a 50-container page still touches the store once.
                "links": hstore.get_links(),
                "major": hstore.get_pending_major() or {},
            }

        def _status_row(self, c, view, t, multi):
            """One `<tr>` of the status table, for the host `view` is about.

            Local and remote rows go through this one function — that is
            what makes "a remote row has the same buttons" true by
            construction rather than by two lists of features drifting
            apart. Every form it emits carries the container's HOST KEY
            (`container_store.host_key`: `nginx` locally, `nas/nginx`
            remotely) in the `name` field, which is the identifier the
            Telegram callbacks already use. On a single-host install that
            key IS the bare name, so the markup below is unchanged to the
            byte.
            """
            from update_checker import UpdateChecker as _UC
            from container_store import LOCAL_HOST, host_key
            host_name = view["host"]
            hstore = view["store"]
            own_name = view["own_name"]
            pending_names = view["pending_names"]
            pinned = view["pinned"]
            auto_list = view["auto_list"]
            ask_major = view["ask_major"]
            groups_lookup = view["groups"]
            notes_lookup = view["notes"]
            links_lookup = view["links"]
            host_td = (f'\n<td class="host-cell">{_e(host_name)}</td>'
                       if multi else "")
            row_open = (f'<tr data-host="{_e(host_name)}">' if multi
                        else "<tr>")
            # Are we looking at our own row? Determined up front because
            # the Auto column, the badges and the action buttons all need
            # it. `own_name` is empty on hosts where self-detection can't
            # resolve a name (QNAP/Podman corner cases, see
            # scripts/test_self_detection.py) — the `own_name and` guard
            # keeps that case on exactly the old behaviour instead of
            # matching every container against "".
            is_self = bool(own_name) and c["name"] == own_name
            health = c.get("health", "")
            if health == "healthy":
                status_badge = '<span class="badge badge-green">healthy</span>'
            elif health == "unhealthy":
                status_badge = '<span class="badge badge-red">unhealthy</span>'
            elif health == "starting":
                status_badge = '<span class="badge badge-yellow">starting</span>'
            else:
                status_badge = '<span class="badge badge-blue">running</span>'

            # Effective states: a docksentry.* label overrides the
            # stored toggle (#42, @LeeNX) — the table must show what
            # actually applies, not just what was clicked in the UI.
            # The 🏷 marker tells the user a label is authoritative
            # (LeeNX's follow-up: make that visible), and the matching
            # toggle buttons are disabled — a click couldn't override
            # the label anyway, pretending otherwise would be a lie.
            _lab_auto = _UC.label_bool(c.get("labels"), "auto")
            _lab_pin = _UC.label_bool(c.get("labels"), "pin")
            _lab_protect = _UC.label_bool(c.get("labels"), "protect")
            if is_self:
                # Our own updates are governed by AUTO_SELFUPDATE and by
                # nothing else (#51, @LeeNX): the opt-in list is skipped
                # for ourselves by the update flow, and main.py strips our
                # name from it on every boot. _lab_auto is IGNORED here on
                # purpose — a docksentry.auto label on the Docksentry
                # container describes how *another* instance would treat
                # this container, and no such instance is watching us.
                # Letting it win over AUTO_SELFUPDATE would show a state
                # that never applies; that mix-up is the heart of #51.
                is_auto = bool(config.auto_selfupdate)
            else:
                is_auto = _lab_auto if _lab_auto is not None else (c["name"] in auto_list)
            is_pinned_c = _lab_pin if _lab_pin is not None else (c["name"] in pinned)
            _protected_c = (_lab_protect if _lab_protect is not None
                            else hstore.is_protect_stop(c["name"]))
            _lab_mark = (f' <span class="label-mark" '
                         f'title="{_e(t("web_label_authoritative"))}">🏷</span>')
            # Our own row gets its own marker — NOT 🏷. That one says
            # "a compose label decides this, you can't change it here";
            # here a setting decides it, and it very much is changeable,
            # just under Settings › Updates.
            _self_mark = (f' <span class="self-mark" '
                          f'title="{_e(t("web_selfupdate_marker_tt"))}">⚙</span>')

            # Badges (compact, only show what's "different" from default)
            badges = ""
            if c["name"] in pending_names:
                badges += f' <span class="badge badge-yellow" title="{_e(t("web_badge_update_tt"))}">{t("web_badge_update")}</span>'
            if is_pinned_c:
                badges += f' <span class="badge badge-red" title="{_e(t("web_badge_pinned_tt"))}">{t("web_pinned_badge")}</span>'
                if _lab_pin is not None:
                    badges += _lab_mark
            if _protected_c:
                badges += f' <span class="badge badge-blue" title="{_e(t("web_protect_stop"))}">🛡</span>'
                if _lab_protect is not None:
                    badges += _lab_mark
            # Auto-update now has its own table column (#2, @NotRetarded) —
            # no longer a name-cell badge that wrapped under long names.
            if c["name"] in ask_major:
                badges += f' <span class="badge badge-blue" title="{_e(t("web_badge_major_tt"))}">{_ICONS["alert"]}</span>'
            if c["name"] in groups_lookup:
                gid, gname = groups_lookup[c["name"]]
                badges += f' <span class="badge badge-purple" title="{_e(t("web_badge_group_tt", group=gname))}">{_icon_label("package", _e(gname))}</span>'
            if c["name"] in notes_lookup:
                note_text = notes_lookup[c["name"]]
                badges += f' <span class="note-icon" title="{_e(note_text)}">📝</span>'
            # Repo / changelog link (#52, @LeeNX): "Not all my
            # Docksentry instances have Telegram or webhook
            # integration" — so the URL that notifications wrap
            # around the name has to be reachable from the table
            # too. Resolved from labels already in hand, no extra
            # docker call; see _row_link.
            _link_url, _link_kind = self._row_link(c, links_lookup)
            # …but only where the URL actually leads somewhere the
            # user asked for. `registry` is our own guess at an
            # overview page derived from the image reference — a
            # Docker Hub landing page, not a changelog. On this host
            # that guess covers 12 of 19 containers, so showing it
            # would put an icon on nearly every row and have most of
            # them lead somewhere LeeNX didn't ask to go. The table
            # is also the exact place he's twice asked us to keep
            # quieter (#37, #46). The guess still stands in Telegram
            # and on the container page, where there's room to
            # explain it.
            if _link_kind == "registry":
                _link_url = ""
            # Styled inline instead of via a CSS class: the same
            # discreet look as .note-icon, minus its `cursor: help`
            # (this one is genuinely clickable) and minus the
            # default link underline.
            _link_a = self._link_anchor(
                t, _link_url, _link_kind,
                attrs='class="row-link" style="opacity:.65;margin-left:4px;'
                      'font-size:12px;text-decoration:none"')
            if _link_a:
                badges += f' {_link_a}'

            # Action buttons — icon-only with tooltips. Container name is
            # escaped for safe use in HTML attributes.
            #
            # `name_attr` stays the plain container name — it is what the
            # user reads and what the /container/ URL uses. `key_attr` is
            # the HOST KEY every action form and the bulk checkbox carry,
            # so the endpoint on the other side knows which machine the
            # click was about without ever guessing from the name. The two
            # are the same string for the local host, which is why a
            # single-host page is unchanged to the byte.
            name_attr = _e(c["name"])
            key_attr = _e(host_key(host_name, c["name"]))
            # Dedicated Auto column (#2, @NotRetarded): a clear on/off cell
            # instead of a name-cell badge that wrapped under long names.
            # Our own row reads AUTO_SELFUPDATE, so the tooltip has to
            # say so — "runs on the next scheduled tick" is true, but
            # the user needs to know *which* switch produced this value.
            auto_cell = (
                f'<span class="badge badge-purple" '
                f'title="{_e(t("web_selfupdate_marker_tt") if is_self else t("web_badge_auto_tt"))}">'
                f'{t("web_autoupdate_badge")}</span>'
                if is_auto else '<span class="muted">—</span>')
            if is_self:
                # Marker in both states: "—" on our own row means
                # "self-update is manual", not "nobody clicked the toggle".
                auto_cell += _self_mark
            elif _lab_auto is not None:
                auto_cell += _lab_mark
            is_askm = c["name"] in ask_major
            # Per-container check (#50). Not a form — it talks to
            # /api/check_one via fetch and reports back with a toast,
            # because a redirect would land the user on the same stale
            # page the global check already leaves them on. All labels
            # ride along as data-* attributes; app.js has no translator.
            check_btn = (
                f'<button type="button" class="btn-icon" '
                f'onclick="dsCheckOne(this)" data-name="{key_attr}" '
                f'data-msg-found="{_e(t("web_check_one_found", name=c["name"]))}" '
                f'data-msg-none="{_e(t("web_check_one_none", name=c["name"]))}" '
                f'data-msg-busy="{_e(t("web_check_one_busy"))}" '
                f'data-msg-error="{_e(t("web_check_one_error"))}" '
                f'title="{_e(t("web_check_one_tt"))}">{_ICONS["search"]}</button>'
            )
            update_btn = (
                f'<form method="POST" action="/api/update" class="inline-form">'
                f'<input type="hidden" name="name" value="{key_attr}">'
                f'<button type="submit" class="btn-icon is-active" title="{_e(t("web_update_tt"))}">{_ICONS["refresh"]}</button>'
                f'</form>'
            ) if c["name"] in pending_names else ''
            pin_form_action = "/api/unpin" if is_pinned_c else "/api/pin"
            _pin_disabled = ' disabled' if _lab_pin is not None else ''
            _pin_title = (t("web_label_authoritative") if _lab_pin is not None
                          else (t("web_unpin_tt") if is_pinned_c else t("web_pin_tt")))
            pin_btn = (
                f'<form method="POST" action="{pin_form_action}" class="inline-form">'
                f'<input type="hidden" name="name" value="{key_attr}">'
                f'<button type="submit"{_pin_disabled} class="btn-icon{" is-pinned" if is_pinned_c else ""}" '
                f'title="{_e(_pin_title)}">{_ICONS["pin"]}</button>'
                f'</form>'
            )
            if is_self:
                # No toggle on our own row (#51). It used to render fully
                # active, and a click wrote our name into the opt-in file
                # — where the update flow ignores it (self is skipped) and
                # the migration in main.py silently drops it on the next
                # boot. A button that promises something and then forgets
                # it is worse than no button: link to the switch that
                # actually works instead.
                auto_btn = (
                    f'<a href="/settings#updates" class="btn-icon" '
                    f'title="{_e(t("web_selfupdate_settings_tt"))}">{_ICONS["settings"]}</a>'
                )
            else:
                _auto_disabled = ' disabled' if _lab_auto is not None else ''
                _auto_title = (t("web_label_authoritative") if _lab_auto is not None
                               else (t("web_autoupdate_disable") if is_auto
                                     else t("web_autoupdate_enable")))
                auto_btn = (
                    f'<form method="POST" action="/api/autoupdate" class="inline-form">'
                    f'<input type="hidden" name="name" value="{key_attr}">'
                    f'<button type="submit"{_auto_disabled} class="btn-icon{" is-active" if is_auto else ""}" '
                    f'title="{_e(_auto_title)}">{_ICONS["settings"]}</button>'
                    f'</form>'
                )
            ask_btn = (
                f'<form method="POST" action="/api/ask_major" class="inline-form adv-only">'
                f'<input type="hidden" name="name" value="{key_attr}">'
                f'<button type="submit" class="btn-icon{" is-warn" if is_askm else ""}" '
                f'title="{_e(t("web_ask_major_off") if is_askm else t("web_ask_major_on"))}">{_ICONS["alert"]}</button>'
                f'</form>'
            )
            # Lifecycle buttons (#17). Hidden for our own container —
            # stopping ourselves would kill PID 1 (#16 territory).
            # Restart is shown in both UI modes (low-risk, reversible);
            # Stop is advanced-only because it leaves the container
            # offline until someone starts it back up.
            if is_self:
                restart_btn = ""
                stop_btn = ""
            else:
                restart_btn = (
                    f'<form method="POST" action="/api/lifecycle" class="inline-form">'
                    f'<input type="hidden" name="name" value="{key_attr}">'
                    f'<input type="hidden" name="action" value="restart">'
                    f'<button type="submit" class="btn-icon" title="{_e(t("web_restart_tt"))}">{_ICONS["restart"]}</button>'
                    f'</form>'
                )
                # Stop hidden for protected containers (#38) — restart
                # stays. Effective state: docksentry.protect label wins
                # over the stored toggle (#46, @LeeNX — label-protected
                # containers still showed the Stop button).
                if _protected_c:
                    stop_btn = ""
                else:
                    stop_btn = (
                        f'<form method="POST" action="/api/lifecycle" class="inline-form adv-only" '
                        f'data-confirm="{_e(t("web_lifecycle_confirm_stop", name=c["name"]))}" '
                        # Heading and button label of the confirm modal —
                        # emoji-stripped like everything else the browser
                        # renders as plain text (#46): "🟥 Stop" was
                        # showing up raw in the dialog.
                        f'data-confirm-title="{_e(_strip_emoji(t("lifecycle_btn_stop")))}" '
                        f'data-confirm-label="{_e(_strip_emoji(t("lifecycle_btn_stop")))}" '
                        f'data-confirm-danger="1">'
                        f'<input type="hidden" name="name" value="{key_attr}">'
                        f'<input type="hidden" name="action" value="stop">'
                        f'<button type="submit" class="btn-icon is-danger" title="{_e(t("web_stop_tt"))}">{_ICONS["x"]}</button>'
                        f'</form>'
                    )
            actions = f'<div class="btn-row">{check_btn}{update_btn}{pin_btn}{restart_btn}{stop_btn}{auto_btn}{ask_btn}</div>'

            # Version / hash badge after image — requested in #32 by
            # @LeeNX so you can tell at a glance whether a container
            # is on `v30.0.1` vs `v30.0.2` of an upstream image, when
            # the tag itself (often `latest`) is uninformative. Read
            # from `org.opencontainers.image.version`; falls back to
            # short image ID (12 hex) when the label is absent.
            version_label = c.get("version", "")
            short_id = c.get("short_id", "")
            if version_label:
                version_html = (
                    f'<span class="badge badge-blue" title="OCI image.version label">'
                    f'v{_e(version_label.lstrip("v"))}</span>'
                )
            elif short_id:
                version_html = (
                    f'<span class="badge badge-blue" title="Image short ID (no OCI version label)">'
                    f'<code style="font-size:11px">{_e(short_id)}</code></span>'
                )
            else:
                version_html = ""

            # The detail page (/container/<name>) reads the LOCAL daemon,
            # so only local rows link into it. A remote row shows its name
            # as plain text rather than a link that would quietly describe
            # a different machine's container of the same name.
            name_cell = (f'<a href="/container/{name_attr}" '
                         f'class="container-link">{_e(c["name"])}</a>'
                         if host_name == LOCAL_HOST else _e(c["name"]))
            return f"""{row_open}
<td><input type="checkbox" class="bulk-cb" value="{key_attr}" data-pending="{1 if c["name"] in pending_names else 0}" data-pinned="{1 if is_pinned_c else 0}" data-auto="{1 if is_auto else 0}"></td>
<td>{name_cell}{badges}</td>{host_td}
<td class="image-cell"><code>{_e(c['image'])}</code> {version_html}</td>
<td>{status_badge}</td>
<td>{auto_cell}</td>
<td class="actions-cell">{actions}</td>
</tr>"""

        def _page_status(self):
            from container_store import LOCAL_HOST, host_key
            t = _web_translator(config.language)

            # Multi-host (#7). With more than the local host managed the
            # table gains a Host column plus a host filter, lists every
            # host's containers, and every button on a row acts on THAT
            # row's host. `multi` is None for single-host installs and
            # every fragment below collapses to "" — so their HTML stays
            # byte-for-byte what it was.
            multi = _multi_hosts()
            host_th = f'<th>{t("web_host")}</th>' if multi else ""
            host_cols = 7 if multi else 6

            # Resolve our own container name once per render so we can
            # suppress the Stop/Restart buttons on the row representing
            # ourselves (clicking them would kill the bot — #16).
            try:
                own_name = checker._own_container_name()
            except Exception:
                own_name = ""

            # One view per managed host, local first. A single-host install
            # builds exactly one, from `self._get_containers()` and the raw
            # store — the same two reads the page always did.
            views = [self._status_view(LOCAL_HOST, _store_for(LOCAL_HOST),
                                       self._get_containers(), own_name)]
            for _host in (multi or ()):
                if _host.is_local:
                    continue
                try:
                    # Probe before listing: a `ps` that exits non-zero comes
                    # back as an empty list, and reporting a dead host as
                    # "no containers running" is worse than saying nothing.
                    # The timeout is the other half — an endpoint that never
                    # answers must not hang the page.
                    _probe = _host.backend.ps(fmt="{{.Names}}", timeout=10)
                    if _probe.returncode != 0:
                        raise OSError((_probe.stderr or "").strip() or "ps failed")
                    _remote = self._containers_on(_host.backend, timeout=10)
                except Exception:
                    # One dead host is a line in the table, not a broken page.
                    views.append({"unreachable": _host.name})
                    continue
                views.append(self._status_view(_host.name, _host.store,
                                               _remote, ""))

            rows = ""
            for view in views:
                if view.get("unreachable"):
                    rows += (
                        f'<tr class="host-unreachable" '
                        f'data-host="{_e(view["unreachable"])}">'
                        f'<td colspan="{host_cols}" class="muted">'
                        f'{_e(t("web_host_unreachable", host=view["unreachable"]))}</td>'
                        f'</tr>'
                    )
                    continue
                for c in view["containers"]:
                    rows += self._status_row(c, view, t, multi)

            # Single-host: `views` holds exactly one entry, so both counts
            # are the expressions they were before multi-host existed.
            total_count = sum(len(v.get("containers") or ()) for v in views)
            pending_count = sum(len(v.get("pending") or ()) for v in views)

            # Host filter (#7). Same client-side idea as the search box
            # right next to it: it hides rows, it does not reload. Rendered
            # only when there is more than one host to choose between.
            host_filter = ""
            host_filter_js = ""
            if multi:
                _opts = "".join(
                    f'<option value="{_e(h.name)}">{_e(h.name)}</option>'
                    for h in multi)
                host_filter = (
                    f'<select id="hostFilter" class="host-filter" '
                    f'title="{_e(t("web_host"))}" '
                    f'aria-label="{_e(t("web_host"))}">'
                    f'<option value="">{_e(t("web_host_filter_all"))}</option>'
                    f'{_opts}</select>')
                host_filter_js = self._host_filter_js(t, total_count)

            major_banner = ""
            rows_mp = ""
            for view in views:
                if view.get("unreachable"):
                    continue
                for n, info in (view["major"] or {}).items():
                    # Same host key as everywhere else — the confirm button
                    # has to resume the update on the box it was held back
                    # on, and `_confirm_major_update` splits exactly this.
                    _mp_key = _e(host_key(view["host"], n))
                    _mp_tag = (f' <span class="muted">@{_e(view["host"])}</span>'
                               if multi else "")
                    rows_mp += f"""<tr>
<td><span style="color:var(--warn);vertical-align:middle">{_ICONS["alert"]}</span> <code>{_e(n)}</code>{_mp_tag}</td>
<td><code>{_e(info.get('old_version',''))} → {_e(info.get('new_version',''))}</code></td>
<td>
<form method="POST" action="/api/major_confirm" class="inline-form">
<input type="hidden" name="name" value="{_mp_key}">
<input type="hidden" name="action" value="confirm">
<button type="submit" class="btn-sm btn">{t("web_major_confirm")}</button>
</form>
<form method="POST" action="/api/major_confirm" class="inline-form" style="margin-left:6px">
<input type="hidden" name="name" value="{_mp_key}">
<input type="hidden" name="action" value="reject">
<button type="submit" class="btn-sm btn-outline">{t("web_major_reject")}</button>
</form>
</td>
</tr>"""
            if rows_mp:
                major_banner = f"""<div class="card card-warn">
<h2>{_ICONS["alert"]} {t("web_major_pending_title")}</h2>
<p class="card-intro">{t("web_major_pending_intro")}</p>
<table>{rows_mp}</table>
</div>"""

            # Last-check timestamp from update_history.json
            last_check_text = t("web_stat_last_check_never")
            try:
                if os.path.exists(config.history_file):
                    with open(config.history_file) as f:
                        hist = json.load(f)
                    if hist:
                        last_ts_raw = hist[-1].get("timestamp", "")
                        if last_ts_raw:
                            from datetime import datetime as _dt
                            try:
                                last_ts = _dt.strptime(last_ts_raw, "%Y-%m-%d %H:%M:%S")
                                delta = _dt.now() - last_ts
                                if delta.total_seconds() < 60:
                                    last_check_text = t("web_stat_just_now")
                                elif delta.total_seconds() < 3600:
                                    last_check_text = t("web_stat_minutes_ago", n=int(delta.total_seconds() // 60))
                                elif delta.total_seconds() < 86400:
                                    last_check_text = t("web_stat_hours_ago", n=int(delta.total_seconds() // 3600))
                                else:
                                    last_check_text = t("web_stat_days_ago", n=int(delta.total_seconds() // 86400))
                            except ValueError:
                                pass
            except (json.JSONDecodeError, IOError):
                pass

            # Disk usage stat
            try:
                disk_pct, disk_free, disk_total = checker.get_disk_usage()
                disk_free_gb = disk_free / 1024**3
                disk_color = "var(--danger)" if disk_pct >= 90 else (
                    "var(--warn)" if disk_pct >= (config.disk_warn_percent or 85) else "var(--accent)")
                disk_stat = f"""<div class="card stat">
    <div class="num" style="color:{disk_color}">{disk_pct:.0f}%</div>
    <div class="label">{t("web_stat_disk_usage", free=f"{disk_free_gb:.0f}G")}</div>
</div>"""
            except Exception:
                disk_stat = ""

            content = f"""
{major_banner}
<div class="stat-grid">
<div class="card stat">
    <div class="num">{total_count}</div>
    <div class="label">{t("web_containers")}</div>
</div>
<div class="card stat">
    <div class="num"{' style="color:var(--warn)"' if pending_count else ''}>{pending_count}</div>
    <div class="label">{t("web_updates_available")}</div>
</div>
<div class="card stat">
    <div class="num" style="font-size:18px;line-height:1.5;padding-top:6px">{last_check_text}</div>
    <div class="label">{t("web_stat_last_update")}</div>
</div>
{disk_stat}
</div>"""
            content += f"""

<div class="card">
<div class="card-header-row">
<h2 style="margin:0">{t("web_containers")}</h2>
<a href="/api/check" class="btn btn-blue btn-compact btn-icon-text">{_ICONS["search"]}<span>{_strip_emoji(t("web_check_updates"))}</span></a>
</div>
<div class="toolbar-row">
<input type="text" id="containerSearch" class="search-input" placeholder="{_e(t('web_search_placeholder'))}">
{host_filter}<span class="row-info" id="containerCount">{t("web_containers_running", count=total_count)}</span>
</div>
<form id="bulkForm" method="POST" action="/api/bulk" class="bulk-bar">
<input type="hidden" name="action" id="bulkAction" value="">
<input type="hidden" name="names" id="bulkNames" value="">
<span id="bulkCount" class="bulk-count">{t("web_bulk_none_selected")}</span>
<span class="bulk-divider"></span>
<button type="button" class="btn-sm btn btn-icon-text" onclick="bulkSubmit('update')" title="{_e(t('web_bulk_update_tt'))}">{_ICONS["refresh"]}<span>{t("web_bulk_update")}</span></button>
<button type="button" class="btn-sm btn-outline btn-icon-text" onclick="bulkSubmit('pin')" title="{_e(t('web_bulk_pin_tt'))}">{_ICONS["pin"]}<span>{t("web_bulk_pin")}</span></button>
<button type="button" class="btn-sm btn-outline btn-icon-text" onclick="bulkSubmit('unpin')" title="{_e(t('web_bulk_unpin_tt'))}">{_ICONS["pin"]}<span>{t("web_bulk_unpin")}</span></button>
<button type="button" class="btn-sm btn-outline btn-icon-text" onclick="bulkSubmit('autoupdate_on')" title="{_e(t('web_bulk_auto_on_tt'))}">{_ICONS["settings"]}<span>{t("web_bulk_auto_on")}</span></button>
<button type="button" class="btn-sm btn-outline btn-icon-text" onclick="bulkSubmit('autoupdate_off')" title="{_e(t('web_bulk_auto_off_tt'))}">{_ICONS["settings"]}<span>{t("web_bulk_auto_off")}</span></button>
</form>
<table id="ctbl">
<thead><tr><th><input type="checkbox" id="bulkSelectAll" style="width:auto" title="{t("web_bulk_select_all")}"></th><th class="sortable" onclick="sortByName()" title="{t('web_sort_name')}" style="cursor:pointer;user-select:none">{t("web_name")} <span id="nameSortArrow"></span></th>{host_th}<th>{t("web_image")}</th><th>{t("web_status")}</th><th>{t("web_autoupdate_badge")}</th><th>{t("web_actions")}</th></tr></thead>
<tbody id="ctblBody">
{rows}
</tbody>
</table>
<!-- Legend keys: _legend_word() strips the Telegram emoji and upper-cases
     the first letter, so the row no longer mixes "Update" with a lowercase
     "auto"/"major-confirm"/"label" (#46, @LeeNX). The last two used to be
     hardcoded English — they're translated keys now. Tooltips carry the
     same substantial *_tt texts as the real buttons. -->
<div class="icon-legend" aria-label="button legend">
<span title="{_e(t("web_check_one_tt"))}"><span class="btn-icon">{_ICONS["search"]}</span> {_legend_word(t("web_check_one"))}</span>
<span title="{_e(t("web_update_tt"))}"><span class="btn-icon is-active">{_ICONS["refresh"]}</span> {_legend_word(t("web_update"))}</span>
<span title="{_e(t("web_restart_tt"))}"><span class="btn-icon">{_ICONS["restart"]}</span> {_legend_word(t("lifecycle_btn_restart"))}</span>
<span title="{_e(t("web_pin_tt"))}"><span class="btn-icon">{_ICONS["pin"]}</span> {_legend_word(t("web_pin"))}</span>
<span title="{_e(t("web_badge_auto_tt"))}"><span class="btn-icon">{_ICONS["settings"]}</span> {_legend_word(t("web_autoupdate_badge"))}</span>
<span title="{_e(t("web_badge_major_tt"))}"><span class="btn-icon">{_ICONS["alert"]}</span> {_legend_word(t("web_legend_major_confirm"))}</span>
<span title="{_e(t("web_stop_tt"))}"><span class="btn-icon is-danger">{_ICONS["x"]}</span> {_legend_word(t("lifecycle_btn_stop"))}</span>
<span title="{_e(t("web_label_authoritative"))}">🏷 {_legend_word(t("web_legend_label"))}</span>
<span title="{_e(t("web_selfupdate_marker_tt"))}">⚙ {_legend_word(t("web_legend_selfupdate"))}</span>
<span title="{_e(t("web_link_open_tt"))}">🔗 {_legend_word(t("web_link_title"))}</span>
</div>
</div>
<script>
(function() {{
    const cbAll = document.getElementById('bulkSelectAll');
    const cbs = document.querySelectorAll('.bulk-cb');
    const countEl = document.getElementById('bulkCount');
    const bar = document.getElementById('bulkForm');
    const btns = bar.querySelectorAll('button[onclick]');

    function selectedCbs() {{
        return Array.from(cbs).filter(c => c.checked);
    }}
    function selectedNames() {{
        return selectedCbs().map(c => c.value);
    }}
    function refresh() {{
        const sel = selectedCbs();
        const n = sel.length;
        countEl.textContent = n === 0 ? '{t("web_bulk_none_selected")}'
                                       : n + ' {t("web_bulk_selected_suffix")}';
        bar.classList.toggle('is-active', n > 0);

        // Smart-disable: every button knows what action it triggers from
        // its onclick. Disable buttons that wouldn't change anything for
        // the current selection.
        const allPending  = n > 0 && sel.every(c => c.dataset.pending === '1');
        const allPinned   = n > 0 && sel.every(c => c.dataset.pinned  === '1');
        const nonePinned  = n > 0 && sel.every(c => c.dataset.pinned  === '0');
        const allAuto     = n > 0 && sel.every(c => c.dataset.auto    === '1');
        const noneAuto    = n > 0 && sel.every(c => c.dataset.auto    === '0');

        btns.forEach(b => {{
            const m = (b.getAttribute('onclick') || '').match(/bulkSubmit\\('([^']+)'\\)/);
            const action = m ? m[1] : null;
            let disabled = (n === 0);
            if (n > 0 && action === 'update'         && !allPending) disabled = true;
            if (n > 0 && action === 'pin'            && allPinned)   disabled = true;
            if (n > 0 && action === 'unpin'          && nonePinned)  disabled = true;
            if (n > 0 && action === 'autoupdate_on'  && allAuto)     disabled = true;
            if (n > 0 && action === 'autoupdate_off' && noneAuto)    disabled = true;
            b.disabled = disabled;
        }});
    }}
    cbAll.addEventListener('change', () => {{
        cbs.forEach(c => c.checked = cbAll.checked);
        refresh();
    }});
    cbs.forEach(c => c.addEventListener('change', refresh));

    // Live search/filter on the container table
    const searchEl = document.getElementById('containerSearch');
    const countInfo = document.getElementById('containerCount');
    const allRows = Array.from(document.querySelectorAll('table tr')).slice(1); // skip header
    function applyFilter() {{
        const q = (searchEl.value || '').toLowerCase().trim();
        let visible = 0;
        allRows.forEach(r => {{
            const text = r.textContent.toLowerCase();
            const match = !q || text.includes(q);
            r.classList.toggle('is-hidden', !match);
            if (match) visible++;
        }});
        if (countInfo) {{
            const total = allRows.length;
            countInfo.textContent = q
                ? visible + ' / ' + total + ' {t("web_containers_match")}'
                : '{t("web_containers_running_short", count=total_count)}';
        }}
    }}
    if (searchEl) {{
        searchEl.addEventListener('input', applyFilter);
    }}

    window.bulkSubmit = function(action) {{
        const names = selectedNames();
        if (names.length === 0) return;
        document.getElementById('bulkAction').value = action;
        document.getElementById('bulkNames').value = names.join(',');
        bar.submit();
    }};
    refresh();
}})();
</script>{host_filter_js}"""

            self._send_html(self._render_page(content, "status"))

        def _host_filter_js(self, t, total_count):
            """The host `<select>`'s behaviour — a SEPARATE script block,
            emitted only when more than one host is managed (#7).

            Deliberately not folded into the table script above: that one
            ships on every install, and touching it would change the bytes
            a single-host page sends. This block registers its own `input`
            listener on the same search field; because it is parsed later
            it also runs later, so its verdict (host AND text must match)
            is the one that sticks on the container rows. Rows belonging
            to other tables on the page are none of its business, hence
            the `#ctblBody` scope.
            """
            return f"""
<script>
(function() {{
    const sel = document.getElementById('hostFilter');
    const searchEl = document.getElementById('containerSearch');
    const countInfo = document.getElementById('containerCount');
    const rows = Array.from(document.querySelectorAll('#ctblBody tr'));
    if (!sel) return;
    // A hidden row must never stay selected: "filter to nas, select all,
    // bulk update" would otherwise update the local containers too, which
    // is the wrong-host accident the whole feature has to not have.
    function dropHidden() {{
        rows.forEach(r => {{
            if (!r.classList.contains('is-hidden')) return;
            r.querySelectorAll('.bulk-cb').forEach(cb => {{
                if (cb.checked) {{
                    cb.checked = false;
                    cb.dispatchEvent(new Event('change', {{bubbles: true}}));
                }}
            }});
        }});
    }}
    function apply() {{
        const host = sel.value;
        const q = (searchEl && searchEl.value || '').toLowerCase().trim();
        let visible = 0;
        rows.forEach(r => {{
            const rh = r.getAttribute('data-host') || '';
            const match = (!host || rh === host)
                       && (!q || r.textContent.toLowerCase().includes(q));
            r.classList.toggle('is-hidden', !match);
            if (match) visible++;
        }});
        if (countInfo) {{
            countInfo.textContent = (host || q)
                ? visible + ' / ' + rows.length + ' {t("web_containers_match")}'
                : '{t("web_containers_running_short", count=total_count)}';
        }}
        dropHidden();
    }}
    sel.addEventListener('change', apply);
    if (searchEl) searchEl.addEventListener('input', apply);
    const selectAll = document.getElementById('bulkSelectAll');
    if (selectAll) selectAll.addEventListener('change', dropHidden);
}})();
</script>"""

        def _page_container(self, name):
            """Per-container detail view: Overview / History / Logs / Settings.

            Tabs persist in localStorage so reloading keeps the user on the
            same view. URL is stable: /container/<name>.
            """
            t = _web_translator(config.language)
            name = name.strip("/")
            if not name:
                self._send_redirect("/")
                return

            # Resolve container info — must exist in `docker ps -a`
            inspect = backend.inspect(name)
            if inspect.returncode != 0:
                content = f"""<div class="card">
<h2>{_e(name)}</h2>
<div class="empty">
<div class="empty-icon">❌</div>
<div class="empty-title">{t("web_container_not_found")}</div>
<div class="empty-hint">{t("web_container_not_found_hint")}</div>
</div>
<a href="/" class="btn btn-outline" style="margin-top:8px;display:inline-block">← {t("web_back_to_status")}</a>
</div>"""
                self._send_html(self._render_page(content, "status"))
                return

            try:
                meta = json.loads(inspect.stdout)[0]
            except (json.JSONDecodeError, IndexError):
                self._send_redirect("/")
                return

            image = meta.get("Config", {}).get("Image", "?")
            state = meta.get("State", {})
            status_state = state.get("Status", "?")
            health = state.get("Health", {}).get("Status", "")
            started_at = state.get("StartedAt", "")[:19].replace("T", " ")
            created = meta.get("Created", "")[:10]

            # Image size — of the image the container is actually RUNNING
            # (meta["Image"], the running image ID), not `image` which is the
            # tag reference. If the tag has moved forward but the container
            # wasn't recreated (the #53 drift), inspecting the tag would report
            # the new image's size instead of the running one. Same class the
            # status table fixed in v1.47.0 and the update check in v1.57.x.
            # Falls back to the tag ref if the running ID is somehow absent.
            running_image = meta.get("Image") or image
            size_bytes = 0
            try:
                size_inspect = backend.image_inspect(running_image, fmt="{{.Size}}")
                if size_inspect.returncode == 0:
                    size_bytes = int(size_inspect.stdout.strip() or 0)
            except (ValueError, subprocess.SubprocessError):
                pass
            if size_bytes >= 1073741824:
                size_str = f"{size_bytes/1073741824:.1f} GB"
            elif size_bytes >= 1048576:
                size_str = f"{size_bytes/1048576:.0f} MB"
            else:
                size_str = f"{size_bytes/1024:.0f} KB" if size_bytes else "?"

            # Status badge
            if "healthy" in (health or "").lower():
                status_badge = '<span class="badge badge-green">healthy</span>'
            elif "starting" in (health or "").lower():
                status_badge = '<span class="badge badge-yellow">starting</span>'
            elif status_state == "running":
                status_badge = '<span class="badge badge-blue">running</span>'
            elif status_state == "exited":
                status_badge = '<span class="badge badge-red">exited</span>'
            else:
                status_badge = f'<span class="badge badge-blue">{_e(status_state)}</span>'

            # Per-container flags
            is_pinned_c = store.is_pinned(name)
            # Same rule as the status table (#51): for our own container the
            # auto-update state is AUTO_SELFUPDATE, not the opt-in list — that
            # list is skipped for self and cleared on the next boot. Empty
            # own-name (QNAP/Podman) falls through to the old behaviour.
            det_is_self = self._is_own_container(name)
            is_auto = bool(config.auto_selfupdate) if det_is_self else store.is_auto(name)
            is_askm = store.is_ask_before_major(name)
            is_trust_c = store.is_trust_running(name)
            cooldown_c = store.get_cooldown(name)
            # Effective protect: docksentry.protect label wins (#46). When a
            # label controls it, the checkbox is disabled — a click couldn't
            # override the label.
            from update_checker import UpdateChecker as _UC2
            # One inspect for every label this page needs — the protect
            # override AND the `docksentry.link` lock below both read
            # this dict (#52). A second get_container_labels() call would
            # be a second `docker inspect` for data we already have.
            det_labels = checker.get_container_labels(name) or {}
            _det_lab_protect = _UC2.label_bool(det_labels, "protect")
            is_protect_c = (_det_lab_protect if _det_lab_protect is not None
                            else store.is_protect_stop(name))
            window = store.get_update_window(name)

            # ── Repo / changelog link (#52) ───────────────────────
            # One container per page, so the full resolver is affordable
            # here: same chain, same `kind` vocabulary, and — unlike the
            # table — guaranteed to agree with what Telegram/Discord put
            # in a notification, because it IS the notification's code.
            from container_store import is_safe_link as _is_safe_link
            _img_ref = image if image and image != "?" else ""
            if bot is not None:
                det_link_url, det_link_kind = LinkResolver(
                    store, config).resolve_link_with_kind(
                    name, _img_ref, checker)
            else:
                # Headless / test setups without a Telegram bot object.
                det_link_url, det_link_kind = self._row_link(
                    {"name": name, "image": _img_ref, "labels": det_labels},
                    store.get_links())
            # Defence in depth. `resolve_link_with_kind` validates the
            # label but hands the stored override straight through from
            # `container_links.json`, and that file may hold values from
            # before set_link validated anything. Re-check before it can
            # reach an href.
            if det_link_url and not _is_safe_link(det_link_url):
                det_link_url, det_link_kind = "", "none"
            # A `docksentry.link` label outranks anything the form can
            # save. Leaving the form live would let the user store a URL
            # that then never shows up anywhere — the exact lie the 🏷
            # marker exists to prevent, so the form gets disabled instead.
            _det_lab_link = det_labels.get("docksentry.link")
            _det_lab_link = (str(_det_lab_link).strip()
                             if isinstance(_det_lab_link, str) else "")
            link_locked = bool(_det_lab_link) and _is_safe_link(_det_lab_link)
            _lab_link_mark = (f'<span class="label-mark" '
                              f'title="{_e(t("web_label_authoritative"))}">🏷</span>')

            # Pending update for this container?
            pending = self._get_pending()
            pending_for_self = next((u for u in pending if u["name"] == name), None)

            # Update history filtered to this container
            history = []
            if os.path.exists(config.history_file):
                try:
                    with open(config.history_file) as f:
                        all_h = json.load(f)
                    history = [h for h in all_h if h.get("container") == name]
                except (json.JSONDecodeError, IOError):
                    pass

            # Compose info if part of a stack
            compose_info = self._compose_info_for(meta)

            # ── Action bar ────────────────────────────────────────
            action_buttons = []
            if pending_for_self:
                action_buttons.append(
                    f'<form method="POST" action="/api/update" class="inline-form">'
                    f'<input type="hidden" name="name" value="{_e(name)}">'
                    f'<button type="submit" class="btn btn-icon-text">{_ICONS["refresh"]}<span>{t("web_update")}</span></button>'
                    f'</form>'
                )
            pin_action = "/api/unpin" if is_pinned_c else "/api/pin"
            pin_label = t("web_unpin") if is_pinned_c else t("web_pin")
            action_buttons.append(
                f'<form method="POST" action="{pin_action}" class="inline-form">'
                f'<input type="hidden" name="name" value="{_e(name)}">'
                f'<button type="submit" class="btn btn-outline btn-icon-text">{_ICONS["pin"]}<span>{_e(pin_label)}</span></button>'
                f'</form>'
            )
            actions_html = "".join(action_buttons)

            # ── Overview tab ──────────────────────────────────────
            badges = []
            if is_pinned_c:
                badges.append(f'<span class="badge badge-red">{t("web_pinned_badge")}</span>')
            if is_auto:
                badges.append(
                    f'<span class="badge badge-purple" '
                    f'title="{_e(t("web_selfupdate_marker_tt") if det_is_self else t("web_badge_auto_tt"))}">'
                    f'{t("web_autoupdate_badge")}</span>')
            if is_askm:
                badges.append('<span class="badge badge-blue">⚠ major-confirm</span>')
            if is_trust_c:
                badges.append(f'<span class="badge badge-blue">{t("web_trust_running_badge")}</span>')
            if pending_for_self:
                badges.append(f'<span class="badge badge-yellow">{t("web_badge_update")}</span>')
            badges_html = " ".join(badges)

            compose_row = ""
            if compose_info:
                compose_row = (
                    f'<tr><td>{t("web_detail_compose")}</td>'
                    f'<td><code>{_e(compose_info.get("compose_project",""))} / {_e(compose_info.get("compose_service",""))}</code></td></tr>'
                )

            window_row = ""
            if window:
                wd_short = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
                wd_set = set(window.get("weekdays") or [])
                wd_text = ", ".join(wd_short[i] for i in sorted(wd_set)) if wd_set else t("web_window_all_days")
                window_row = (
                    f'<tr><td>{t("web_window")}</td>'
                    f'<td><code>{_e(window.get("start",""))}–{_e(window.get("end",""))}</code> · {_e(wd_text)}</td></tr>'
                )

            # Group membership
            group_row = ""
            gid, gdata = store.get_group_for_container(name)
            if gid and gdata:
                cnames = gdata.get("containers") or []
                pos = cnames.index(name) + 1 if name in cnames else "?"
                group_row = (
                    f'<tr><td>{t("web_detail_group")}</td>'
                    f'<td><span style="color:var(--accent);vertical-align:middle">{_ICONS["package"]}</span> <a href="/settings#groups" style="color:var(--accent);text-decoration:none">{_e(gdata.get("name", gid))}</a> '
                    f'<span style="color:var(--text-muted);font-size:12px">({t("web_detail_group_pos", pos=pos, total=len(cnames))})</span></td></tr>'
                )

            # Repo / changelog row — clickable, with the origin spelled
            # out next to it (#52). Rendered only when we actually have
            # a link, same as the compose / window / group rows above.
            link_row = ""
            _link_a = self._link_anchor(t, det_link_url, det_link_kind,
                                        text=_e(det_link_url))
            if _link_a:
                _origin = self._link_origin_text(t, det_link_kind)
                _origin_html = (f' <span class="muted" style="font-size:12px">'
                                f'({_e(_origin)})</span>' if _origin else "")
                link_row = (
                    f'<tr><td>{t("web_link_title")}</td>'
                    f'<td>🔗 {_link_a}{_origin_html}'
                    f'{" " + _lab_link_mark if link_locked else ""}</td></tr>'
                )

            note_text = store.get_note(name)
            note_html = ""
            if note_text:
                note_html = f"""<div style="margin-top:14px;padding:12px;background:var(--bg);border-left:3px solid var(--warn);border-radius:var(--radius-sm)">
<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">📝 {t("web_note_title")}</div>
<div style="font-size:13px;white-space:pre-wrap">{_e(note_text)}</div>
</div>"""

            # Our own row gets a plain-language line about how it updates
            # itself (#51, @LeeNX) — the container detail view said nothing
            # about AUTO_SELFUPDATE at all.
            selfupdate_row = ""
            if det_is_self:
                selfupdate_row = (
                    f'<tr><td>{t("web_detail_selfupdate")}</td>'
                    f'<td>{t("web_selfupdate_auto") if is_auto else t("web_selfupdate_manual")}'
                    f' <span class="self-mark" title="{_e(t("web_selfupdate_marker_tt"))}">⚙</span>'
                    f' · <a href="/settings#updates" class="settings-link">'
                    f'{t("web_selfupdate_open_settings")}</a></td></tr>'
                )

            overview_html = f"""<table>
<tr><td style="width:30%">{t("web_detail_image")}</td><td><code>{_e(image)}</code></td></tr>
<tr><td>{t("web_detail_status")}</td><td>{status_badge} {badges_html}</td></tr>
<tr><td>{t("web_detail_size")}</td><td>{_e(size_str)}</td></tr>
<tr><td>{t("web_detail_created")}</td><td>{_e(created)}</td></tr>
<tr><td>{t("web_detail_started")}</td><td>{_e(started_at)}</td></tr>
{selfupdate_row}
{compose_row}
{window_row}
{group_row}
{link_row}
</table>
{note_html}"""

            # ── History tab ──────────────────────────────────────
            if history:
                hist_rows = ""
                for h in reversed(history[-50:]):
                    icon = '✅' if h.get("success") else '❌'
                    # Normalize legacy v1.16.1 calendar glyph (see CHANGELOG v1.16.2)
                    detail = _strip_md(h.get("detail", "").replace("📅", "🗓️"))
                    hist_rows += (
                        f'<tr><td>{_e(h.get("timestamp",""))}</td>'
                        f'<td>{icon}</td>'
                        f'<td style="font-size:12px">{_e(detail)}</td></tr>'
                    )
                history_html = f"""<table>
<tr><th>{t("web_date")}</th><th>{t("web_result")}</th><th>{t("web_detail")}</th></tr>
{hist_rows}
</table>"""
            else:
                history_html = f"""<div class="empty">
<div class="empty-icon">📋</div>
<div class="empty-title">{t("web_container_history_empty")}</div>
<div class="empty-hint">{t("web_container_history_empty_hint")}</div>
</div>"""

            # ── Logs tab — fetched on demand, not pre-rendered ────
            # Same .logs-filter row as the Logs page — see app.css for why the
            # field margins have to be zeroed inside it (#46).
            logs_html = f"""<form method="GET" action="/container/{_e(name)}" class="logs-filter">
<input type="hidden" name="tab" value="logs">
<div class="logs-filter-grow">
<label>{t("web_logs_lines")}</label>
<input type="number" name="lines" value="100" min="10" max="500">
</div>
<button type="submit" class="btn btn-blue">{t("web_logs_show")}</button>
</form>"""
            query = parse_qs(urlparse(self.path).query)
            if query.get("tab", [""])[0] == "logs":
                lines = max(10, min(int(query.get("lines", ["100"])[0]), 500))
                logs_result = backend.logs(name, tail=lines, timeout=10)
                output = logs_result.stdout or logs_result.stderr
                if output.strip():
                    logs_html += f'<pre>{html.escape(output.strip())}</pre>'
                else:
                    logs_html += '<p style="color:var(--text-muted)">No logs found.</p>'

            # ── Settings tab — per-container toggles ─────────────
            # Feedback for the /api/link POST (#52). That handler used to
            # throw away a rejected URL without a word — the field just
            # came back empty and the user was left guessing. There is no
            # server-side toast infrastructure (app.js only reacts to
            # `?saved=1`), so the answer is rendered inline, right above
            # the field it belongs to.
            _link_msg = (query.get("link", [""])[0] or "").strip()
            _link_notice_map = {
                "saved": ("var(--success)", t("web_link_saved")),
                "cleared": ("var(--text-muted)", t("web_link_cleared")),
                "rejected": ("var(--danger)", t("web_link_rejected")),
            }
            link_notice = ""
            if _link_msg in _link_notice_map:
                _colour, _text = _link_notice_map[_link_msg]
                link_notice = (
                    f'<div style="margin-bottom:8px;padding:8px 10px;'
                    f'background:var(--bg);border-left:3px solid {_colour};'
                    f'border-radius:var(--radius-sm);font-size:13px">'
                    f'{_e(_text)}</div>'
                )
            window_form = self._container_window_form(t, name, window)
            # Our own container: the auto-update checkbox did nothing here
            # either — it posts to /api/autoupdate, which is now guarded, and
            # the opt-in list has never governed our own updates. Replaced by
            # the same statement + link the Status table shows (#51).
            if det_is_self:
                auto_block = f"""<div class="form-checkbox-row">
  <span>{t("web_detail_selfupdate")}: <strong>{t("web_selfupdate_auto") if is_auto else t("web_selfupdate_manual")}</strong>
  <span class="self-mark" title="{_e(t("web_selfupdate_marker_tt"))}">⚙</span></span>
  <a href="/settings#updates" class="settings-link">{t("web_selfupdate_open_settings")}</a>
</div>
<p class="form-help">{t("web_detail_selfupdate_hint")}</p>
"""
            else:
                auto_block = f"""<div class="form-checkbox-row">
  <input type="checkbox" id="cb-detail-auto" {'checked' if is_auto else ''} onchange="document.getElementById('frm-detail-auto').submit()">
  <label for="cb-detail-auto">{t("web_autoupdate_enable")}</label>
</div>
<form id="frm-detail-auto" method="POST" action="/api/autoupdate" class="inline-form">
<input type="hidden" name="name" value="{_e(name)}">
</form>
<p class="form-help">{t("web_detail_auto_hint")}</p>
"""
            settings_html = f"""{auto_block}

<div class="form-checkbox-row">
  <input type="checkbox" id="cb-detail-major" {'checked' if is_askm else ''} onchange="document.getElementById('frm-detail-major').submit()">
  <label for="cb-detail-major">{t("web_ask_major_on")}</label>
</div>
<form id="frm-detail-major" method="POST" action="/api/ask_major" class="inline-form">
<input type="hidden" name="name" value="{_e(name)}">
</form>
<p class="form-help">{t("web_detail_major_hint")}</p>

<div class="form-checkbox-row">
  <input type="checkbox" id="cb-detail-trust" {'checked' if is_trust_c else ''} onchange="document.getElementById('frm-detail-trust').submit()">
  <label for="cb-detail-trust">{t("web_trust_running")}</label>
</div>
<form id="frm-detail-trust" method="POST" action="/api/trust_running" class="inline-form">
<input type="hidden" name="name" value="{_e(name)}">
</form>
<p class="form-help">{t("web_detail_trust_hint")}</p>

<div class="form-checkbox-row">
  <input type="checkbox" id="cb-detail-protect" {'checked' if is_protect_c else ''} {'disabled' if _det_lab_protect is not None else ''} onchange="document.getElementById('frm-detail-protect').submit()">
  <label for="cb-detail-protect" {'title="' + _e(t("web_label_authoritative")) + '"' if _det_lab_protect is not None else ''}>{t("web_protect_stop")}{' 🏷' if _det_lab_protect is not None else ''}</label>
</div>
<form id="frm-detail-protect" method="POST" action="/api/protect" class="inline-form">
<input type="hidden" name="name" value="{_e(name)}">
</form>
<p class="form-help">{t("web_detail_protect_hint")}</p>

<div class="form-checkbox-row adv-only" style="gap:8px">
  <label for="inp-detail-cooldown" style="margin:0">{t("web_cooldown_label")}</label>
  <form method="POST" action="/api/cooldown" class="inline-form" style="display:flex;gap:6px;align-items:center;margin:0">
    <input type="hidden" name="name" value="{_e(name)}">
    <input type="number" id="inp-detail-cooldown" name="seconds" min="0" max="600" value="{cooldown_c}" style="width:80px">
    <button type="submit" class="btn btn-sm">{t("web_cooldown_save")}</button>
  </form>
</div>
<p class="form-help adv-only">{t("web_detail_cooldown_hint")}</p>

<div class="form-checkbox-row">
  <input type="checkbox" id="cb-detail-pinned" {'checked' if is_pinned_c else ''} onchange="document.getElementById('frm-detail-pin').submit()">
  <label for="cb-detail-pinned">{t("web_pin")}</label>
</div>
<form id="frm-detail-pin" method="POST" action="{pin_action}" class="inline-form">
<input type="hidden" name="name" value="{_e(name)}">
</form>
<p class="form-help">{t("web_detail_pin_hint")}</p>

<hr class="section-divider">

<h3 style="font-size:14px;color:var(--accent);margin-bottom:8px">{t("web_note_title")}</h3>
<p class="form-help" style="margin-bottom:8px">{t("web_note_intro")}</p>
<form method="POST" action="/api/note">
<input type="hidden" name="name" value="{_e(name)}">
<textarea name="note" rows="3" placeholder="{_e(t('web_note_placeholder'))}" maxlength="2000" style="width:100%;font-family:inherit;resize:vertical">{_e(store.get_note(name))}</textarea>
<button type="submit" class="btn btn-sm" style="margin-top:6px">{t("web_note_save")}</button>
</form>

<hr class="section-divider">

<h3 style="font-size:14px;color:var(--accent);margin-bottom:8px">{t("web_link_title")}{" " + _lab_link_mark if link_locked else ""}</h3>
<p class="form-help" style="margin-bottom:8px">{t("web_link_intro")}</p>
{link_notice}
<form method="POST" action="/api/link">
<input type="hidden" name="name" value="{_e(name)}">
<input type="url" name="url" placeholder="{_e(t('web_link_placeholder'))}" value="{_e(_det_lab_link if link_locked else store.get_link(name))}" style="width:100%"{' disabled title="' + _e(t("web_label_authoritative")) + '"' if link_locked else ''}>
<button type="submit" class="btn btn-sm" style="margin-top:6px"{' disabled title="' + _e(t("web_label_authoritative")) + '"' if link_locked else ''}>{t("web_link_save")}</button>
</form>

<hr class="section-divider">

<h3 style="font-size:14px;color:var(--accent);margin-bottom:8px">{t("web_window")}</h3>
<p class="form-help" style="margin-bottom:12px">{t("web_detail_window_intro")}</p>
{window_form}"""

            # ── Page assembly ─────────────────────────────────────
            content = f"""
<div class="card">
<div class="card-header-row">
<div>
<a href="/" class="btn-back">← {t("web_back_to_status")}</a>
<h2 style="margin-top:8px;display:flex;align-items:center;gap:10px">{_e(name)} {badges_html}</h2>
</div>
<div class="btn-row">{actions_html}</div>
</div>

<div class="tabs" data-tabs="container">
  <button type="button" class="tab-btn" data-tab-target="overview">{_ICONS["search"]}<span>{t("web_tab_overview")}</span></button>
  <button type="button" class="tab-btn" data-tab-target="history">{_ICONS["calendar"]}<span>{t("web_tab_history")}</span></button>
  <button type="button" class="tab-btn" data-tab-target="logs">{_ICONS["search"]}<span>{t("web_tab_logs")}</span></button>
  <button type="button" class="tab-btn" data-tab-target="cset">{_ICONS["settings"]}<span>{t("web_tab_settings")}</span></button>
</div>

<div class="tab-pane" data-tab-pane="container" data-tab-name="overview">
{overview_html}
</div>
<div class="tab-pane" data-tab-pane="container" data-tab-name="history">
{history_html}
</div>
<div class="tab-pane" data-tab-pane="container" data-tab-name="logs">
{logs_html}
</div>
<div class="tab-pane" data-tab-pane="container" data-tab-name="cset">
{settings_html}
</div>
</div>"""

            self._send_html(self._render_page(content, "status"))

        def _compose_info_for(self, meta):
            """Extract compose project/service/file from a docker-inspect result."""
            labels = meta.get("Config", {}).get("Labels", {}) or {}
            project = labels.get("com.docker.compose.project", "")
            service = labels.get("com.docker.compose.service", "")
            if not project:
                return {}
            return {
                "compose_project": project,
                "compose_service": service,
                "compose_file": labels.get("com.docker.compose.project.config_files", ""),
            }

        def _container_window_form(self, t, name, window):
            """Render the per-container update-window editor (subset of the
            global Update-Windows section, scoped to one container)."""
            wd_full = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            wd_set = set((window or {}).get("weekdays") or [])
            wd_html = ""
            for i, label in enumerate(wd_full):
                checked = "checked" if i in wd_set else ""
                wd_html += (f'<label style="display:inline-block;margin-right:10px;font-size:13px">'
                            f'<input type="checkbox" name="weekdays" value="{i}" {checked} '
                            f'style="width:auto;margin-right:4px">{label}</label>')
            current_start = (window or {}).get("start", "")
            current_end = (window or {}).get("end", "")
            return f"""<form method="POST" action="/api/window">
<input type="hidden" name="name" value="{_e(name)}">
<input type="hidden" name="action" value="save">
<div class="grid">
<div>
<label>{t("web_windows_range")}</label>
<div style="display:flex;gap:8px">
<input type="text" name="start" placeholder="02:00" pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$" value="{_e(current_start)}">
<input type="text" name="end" placeholder="04:00" pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$" value="{_e(current_end)}">
</div>
</div>
<div>
<label>{t("web_windows_days")}</label>
<div>{wd_html}</div>
</div>
</div>
<div style="display:flex;gap:6px;margin-top:8px">
<button type="submit" class="btn btn-sm">{t("web_windows_save")}</button>
<button type="submit" formaction="/api/window" class="btn btn-sm btn-outline" name="action" value="delete">{t("web_delete")}</button>
</div>
</form>"""

        def _page_groups(self):
            """Dedicated Container Groups page (v1.21.0). Promotes
            Container Groups from a hidden tab in Settings to a
            first-class section in the main nav with edit-in-place,
            drag-and-drop reorder, head-container visualisation, and
            stale-member warnings (a member that no longer exists as
            a running or stopped container).

            Storage is unchanged — same store.save_group / .get_groups
            / .delete_group / .reorder_group_container helpers as the
            legacy Settings tab. The legacy tab still works (some users
            have bookmarks) but is now a thin wrapper that links here.
            """
            t = _web_translator(config.language)

            try:
                live = self._get_containers()
            except Exception:
                live = []
            # Include stopped + running so users can group stopped helpers.
            try:
                all_ids = backend.ps(all=True, fmt="{{.Names}}")
                all_names = sorted(set(
                    n for n in all_ids.stdout.strip().split("\n") if n
                ))
            except subprocess.SubprocessError:
                all_names = sorted({c["name"] for c in live})
            live_set = {c["name"] for c in live}
            groups = store.get_groups()

            # Build per-group cards with edit-in-place forms.
            cards_html = ""
            for gid, g in groups.items():
                gname = g.get("name", gid)
                cnames = g.get("containers") or []
                wait_s = int(g.get("wait_seconds", 30) or 30)
                is_rd = bool(g.get("restart_dependents"))

                # Members list with head marker + drag handles + stale warning
                member_rows = ""
                for idx, cname in enumerate(cnames):
                    stale = cname not in (live_set | set(all_names))
                    head_badge = ""
                    if idx == 0:
                        head_badge = (f'<span class="badge badge-purple" '
                                      f'title="{_e(t("web_groups_head_tt"))}">'
                                      f'{t("web_groups_head_badge")}</span>')
                    stale_badge = ""
                    if stale:
                        stale_badge = (f'<span class="badge badge-yellow" '
                                       f'title="{_e(t("web_groups_stale_tt"))}">'
                                       f'{t("web_groups_stale_badge")}</span>')
                    member_rows += (
                        f'<li class="group-member" draggable="true" '
                        f'data-container="{_e(cname)}">'
                        f'<span class="drag-handle" title="{_e(t("web_groups_drag_tt"))}">⠿</span>'
                        f'<code>{_e(cname)}</code> {head_badge}{stale_badge}'
                        f'</li>'
                    )
                if not member_rows:
                    member_rows = (f'<li style="color:var(--text-muted);font-size:12px">'
                                   f'{t("web_groups_no_members")}</li>')

                # Edit form — populated with the current values, posts
                # back with the group_id so the save endpoint updates
                # in place. Container multi-select is pre-selected.
                options_html = ""
                for n in all_names:
                    sel = ' selected' if n in cnames else ''
                    options_html += f'<option value="{_e(n)}"{sel}>{_e(n)}</option>'

                rd_checked = ' checked' if is_rd else ''

                cards_html += f"""
<div class="card" data-group-id="{_e(gid)}" style="margin-bottom:16px">
  <div class="card-header-row">
    <h3 style="font-size:14px;color:var(--accent);margin:0">
      {_ICONS["package"]} {_e(gname)}
      <span style="color:var(--text-muted);font-size:11px;font-weight:400">
        ·  {len(cnames)} {t('web_groups_containers')} · {wait_s}s {t('web_groups_wait')}
        {' · 🔁 ' + t("web_groups_restart_dependents_badge") if is_rd else ''}
      </span>
    </h3>
    <form method="POST" action="/api/group_delete" class="inline-form"
          data-confirm="{_e(t('web_groups_delete_confirm', name=gname))}"
          data-confirm-label="{_e(t('web_delete'))}" data-confirm-danger="1">
      <input type="hidden" name="group_id" value="{_e(gid)}">
      <button type="submit" class="btn-sm btn-outline">{t("web_delete")}</button>
    </form>
  </div>

  <ul class="group-members-list" data-group-id="{_e(gid)}">
    {member_rows}
  </ul>

  <details class="group-edit" style="margin-top:12px">
    <summary style="cursor:pointer;color:var(--accent);font-size:13px">
      {t("web_groups_edit_toggle")}
    </summary>
    <form method="POST" action="/api/group_save" style="margin-top:8px">
      <input type="hidden" name="group_id" value="{_e(gid)}">
      <div class="grid">
        <div>
          <label>{t("web_groups_name")}</label>
          <input type="text" name="name" value="{_e(gname)}" required>
        </div>
        <div>
          <label>{t("web_groups_wait_label")}</label>
          <input type="number" name="wait_seconds" value="{wait_s}" min="0" max="600">
        </div>
      </div>
      <label>{t("web_groups_containers_label")}</label>
      <p class="form-help">{t("web_groups_containers_hint")}</p>
      <select name="containers" multiple size="6" style="height:auto">
        {options_html}
      </select>
      <div class="form-checkbox-row" style="margin-top:8px">
        <input type="checkbox" name="restart_dependents" id="cb-rd-{_e(gid)}"{rd_checked}>
        <label for="cb-rd-{_e(gid)}">{t("web_groups_restart_dependents")}</label>
      </div>
      <p class="form-help">{t("web_groups_restart_dependents_hint")}</p>
      <button type="submit" class="btn" style="margin-top:8px">{t("web_groups_save_changes")}</button>
    </form>
  </details>
</div>"""

            if not groups:
                cards_html = (f'<div class="empty">'
                              f'<div class="empty-icon">📦</div>'
                              f'<div class="empty-title">{t("web_groups_empty")}</div>'
                              f'<div class="empty-hint">{t("web_groups_empty_hint")}</div>'
                              f'</div>')

            # Add-new-group form — same as before but redirects to /groups.
            new_options = "".join(
                f'<option value="{_e(n)}">{_e(n)}</option>' for n in all_names
            )
            new_form = f"""
<div class="card" style="margin-top:24px">
  <h3 style="font-size:14px;color:var(--accent);margin:0 0 12px 0">+ {t("web_groups_new")}</h3>
  <form method="POST" action="/api/group_save">
    <div class="grid">
      <div>
        <label>{t("web_groups_name")}</label>
        <input type="text" name="name" placeholder="{_e(t('web_groups_name_placeholder'))}" required>
      </div>
      <div>
        <label>{t("web_groups_wait_label")}</label>
        <input type="number" name="wait_seconds" value="30" min="0" max="600">
      </div>
    </div>
    <label>{t("web_groups_containers_label")}</label>
    <p class="form-help">{t("web_groups_containers_hint")}</p>
    <select name="containers" multiple size="6" style="height:auto">
      {new_options}
    </select>
    <div class="form-checkbox-row" style="margin-top:8px">
      <input type="checkbox" name="restart_dependents" id="cb-new-rd">
      <label for="cb-new-rd">{t("web_groups_restart_dependents")}</label>
    </div>
    <p class="form-help">{t("web_groups_restart_dependents_hint")}</p>
    <button type="submit" class="btn" style="margin-top:8px">{t("web_groups_save")}</button>
  </form>
</div>"""

            content = f"""
<div class="card">
  <div class="card-header-row" style="align-items:flex-start">
    <div>
      <h2 style="margin:0">{t("web_groups_title")}</h2>
      <p class="card-intro" style="margin-top:6px">{t("web_groups_page_intro")}</p>
    </div>
    <button type="button" class="btn btn-outline" onclick="dsAutoDetectGroups()">
      {t("web_groups_autodetect_button")}
    </button>
  </div>
</div>
{cards_html}
{new_form}

<!-- Auto-detect modal — hidden until dsAutoDetectGroups() opens it -->
<div id="autodetect-modal" class="modal-backdrop" onclick="if(event.target===this)dsAutoDetectClose()">
  <div class="modal" style="max-width:760px;width:92%" onclick="event.stopPropagation()">
    <h3>{t("web_groups_autodetect_title")}</h3>
    <p class="card-intro">{t("web_groups_autodetect_intro")}</p>
    <div id="autodetect-body" style="margin:12px 0;max-height:55vh;overflow-y:auto">
      <div style="text-align:center;color:var(--text-muted);padding:24px">
        {t("web_groups_autodetect_loading")}
      </div>
    </div>
    <div class="modal-actions">
      <button type="button" class="btn-sm btn-outline" onclick="dsAutoDetectClose()">{t("web_cancel")}</button>
      <button type="button" class="btn-sm btn" id="autodetect-import" onclick="dsAutoDetectImport()">
        {t("web_groups_autodetect_import_btn")}
      </button>
    </div>
  </div>
</div>
"""
            self._send_html(self._render_page(content, "groups"))

        def _page_history(self):
            t = _web_translator(config.language)

            history = []
            if os.path.exists(config.history_file):
                try:
                    with open(config.history_file) as f:
                        history = json.load(f)
                except (json.JSONDecodeError, IOError):
                    pass

            # ?container=<name> filter (Bundle 2 / v1.20.0). Lets users
            # zoom into one container's update history without
            # scrolling through the global feed. Empty / "all" shows
            # everything. Mirrors the partial-name resolver semantics
            # used in Telegram: case-insensitive substring match.
            query = parse_qs(urlparse(self.path).query)
            filter_raw = (query.get("container", [""])[0] or "").strip()
            filter_name = filter_raw.lower() if filter_raw and filter_raw.lower() != "all" else ""

            # Build the unique-container dropdown from history data
            # (sorted; case-preserving). Disambiguates self-updates
            # which have container == "docksentry" — only show it once.
            unique_names = sorted(
                {h.get("container", "") for h in history if h.get("container")},
                key=lambda n: n.lower()
            )

            filtered = history
            if filter_name:
                filtered = [
                    h for h in history
                    if filter_name in (h.get("container") or "").lower()
                ]

            # Build the filter form — Status-page link icons in v1.18.0
            # set ?container=<name> via this same URL pattern, so the
            # filter has both UI (dropdown) and deep-link (URL) drivers.
            filter_options = '<option value="all">' + _e(t("web_history_filter_all")) + '</option>\n'
            for n in unique_names:
                sel = 'selected' if n.lower() == filter_name else ''
                filter_options += f'<option value="{_e(n)}" {sel}>{_e(n)}</option>\n'
            filter_form = f"""<form method="GET" action="/history" class="inline-form" style="margin-bottom:12px">
<label style="display:inline-block;margin-right:8px">{t("web_history_filter_label")}:</label>
<select name="container" onchange="this.form.submit()" style="min-width:180px">
{filter_options}
</select>
</form>"""

            if not history:
                content = f"""<div class="card">
<h2>{t("web_history")}</h2>
<div class="empty">
  <div class="empty-icon">📋</div>
  <div class="empty-title">{t("web_history_empty")}</div>
  <div class="empty-hint">{t("web_history_empty_hint")}</div>
</div>
</div>"""
            elif not filtered:
                content = f"""<div class="card">
<h2>{t("web_history")}</h2>
{filter_form}
<div class="empty">
  <div class="empty-icon">🔍</div>
  <div class="empty-title">{t("web_history_filter_empty", name=_e(filter_raw))}</div>
  <div class="empty-hint"><a href="/history">{t("web_history_filter_clear")}</a></div>
</div>
</div>"""
            else:
                rows = ""
                for h in reversed(filtered):
                    icon = '<span class="badge badge-green">✅</span>' if h["success"] else '<span class="badge badge-yellow">❌</span>'
                    # Normalize legacy v1.16.1 calendar glyph (see CHANGELOG v1.16.2)
                    detail = _strip_md(h.get('detail', '').replace('📅', '🗓️'))
                    rows += f"""<tr>
<td>{_e(h.get('timestamp', ''))}</td>
<td>{_e(h.get('container', ''))}{(' <span class="muted">@' + _e(h['host']) + '</span>') if h.get('host') else ''}</td>
<td>{icon}</td>
<td style="font-size:12px">{_e(detail)}</td>
</tr>"""

                count_hint = ""
                if filter_name:
                    count_hint = f'<div style="font-size:12px;color:var(--muted);margin-bottom:8px">{t("web_history_filter_count", shown=len(filtered), total=len(history))}</div>'

                content = f"""<div class="card">
<h2>{t("web_history")}</h2>
{filter_form}
{count_hint}
<table>
<tr><th>{t("web_date")}</th><th>{t("web_name")}</th><th>{t("web_result")}</th><th>{t("web_detail")}</th></tr>
{rows}
</table>
</div>"""

            # ── Monitor events (v1.48.1) ───────────────────────────
            # The monitor's persistent audit trail: what crashed, went
            # unhealthy or got OOM-killed while nobody was watching.
            # Rendered through the same monitor_* i18n keys as the live
            # notifications so both channels tell the same story.
            events = []
            ev_path = getattr(config, "monitor_events_file", None)
            if ev_path and os.path.exists(ev_path):
                try:
                    with open(ev_path) as f:
                        events = json.load(f) or []
                except (json.JSONDecodeError, IOError):
                    events = []
            if events:
                ev_rows = ""
                for ev in reversed(events[-100:]):
                    kind = ev.get("kind", "")
                    try:
                        msg = t(f"monitor_{kind}", name=ev.get("container", "?"),
                                **(ev.get("detail") or {}))
                    except Exception:
                        msg = f"{kind}: {ev.get('container', '?')}"
                    ev_rows += f"""<tr>
<td>{_e(ev.get('timestamp', ''))}</td>
<td>{_e(ev.get('container', ''))}</td>
<td style="font-size:12px">{_e(msg)}</td>
</tr>"""
                content += f"""<div class="card">
<h2>{t("web_events")}</h2>
<table>
<tr><th>{t("web_date")}</th><th>{t("web_name")}</th><th>{t("web_detail")}</th></tr>
{ev_rows}
</table>
</div>"""
            else:
                content += f"""<div class="card">
<h2>{t("web_events")}</h2>
<div class="empty">
  <div class="empty-icon">🩺</div>
  <div class="empty-title">{t("web_events_empty")}</div>
  <div class="empty-hint">{t("web_events_empty_hint")}</div>
</div>
</div>"""

            self._send_html(self._render_page(content, "history"))

        def _page_settings(self):
            from i18n import available_languages
            from version import VERSION
            t = _web_translator(config.language)

            langs = available_languages()
            lang_names = {"en": "English", "de": "Deutsch", "fr": "Français", "es": "Español",
                          "it": "Italiano", "nl": "Nederlands", "pt": "Português", "pl": "Polski",
                          "tr": "Türkçe", "ru": "Русский", "uk": "Українська", "ar": "العربية",
                          "hi": "हिन्दी", "ja": "日本語", "ko": "한국어", "zh": "中文"}
            lang_options = ""
            for l in langs:
                sel = 'selected' if l == config.language else ''
                name = lang_names.get(l, l.upper())
                lang_options += f'<option value="{_e(l)}" {sel}>{_e(name)}</option>\n'

            cb = lambda v: 'checked' if v else ''  # checkbox helper

            # Mask sensitive values
            token_masked = f"{config.bot_token[:4]}...{config.bot_token[-4:]}" if len(config.bot_token) > 8 else "***"
            chat_masked = f"{config.chat_id[:3]}...{config.chat_id[-3:]}" if len(config.chat_id) > 6 else "***"

            telegram_status = 'enabled' if (config.bot_token and config.chat_id) else 'disabled (headless)'

            def help_(text):
                return f'<span class="help" data-tt="{_e(text)}">?</span>'

            def env_(key):
                """Marker for a field whose saved value overrules a set env var.

                Same mechanism as the 🏷 label marker and the ⚙ self-update
                marker — a glyph plus a title — but a third statement: the
                env var is only the starting value, this field owns it now.
                Empty string when nothing is being overruled (#53, @LeeNX).
                """
                o = config.env_override(key)
                if not o:
                    return ""
                # Secrets: name the variable, never its value. o["env"] is
                # None for those (Config._display_value refuses).
                tip = (t("web_env_override_secret_tt", var=o["var"])
                       if o["secret"] else
                       t("web_env_override_tt", var=o["var"], value=o["env"]))
                return f' <span class="env-mark" title="{_e(tip)}">env</span>'

            content = f"""
<div class="card">
<h2>{t("web_settings")}</h2>
<p class="card-intro">{t("web_settings_intro")}</p>

<form method="POST" action="/settings">
<div class="tabs" data-tabs="settings">
  <button type="button" class="tab-btn" data-tab-target="general">{_ICONS["settings"]}<span>{t("web_tab_general")}</span></button>
  <button type="button" class="tab-btn" data-tab-target="updates">{_ICONS["refresh"]}<span>{t("web_tab_updates")}</span></button>
  <button type="button" class="tab-btn" data-tab-target="cleanup">{_ICONS["broom"]}<span>{t("web_tab_cleanup")}</span></button>
  <button type="button" class="tab-btn" data-tab-target="notifs">{_ICONS["alert"]}<span>{t("web_tab_notifications")}</span></button>
  <button type="button" class="tab-btn" data-tab-target="channels">{_ICONS["calendar"]}<span>{t("web_tab_channels")}</span></button>
</div>

<!-- ── Allgemein ─────────────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="general">
  <div class="grid">
    <div>
      <label>{t("web_language")}{env_("language")}</label>
      <select name="language">{lang_options}</select>
    </div>
    <div>
      <label>{t("web_cron_schedule")} {help_(t("web_cron_help"))}{env_("cron_schedule")}</label>
      <input type="text" name="cron_schedule" id="f-cron_schedule" value="{_e(config.cron_schedule)}" oninput="dsCronPreview()">
      <div id="cron-preview" style="font-size:11px;color:var(--muted);margin-top:4px;min-height:14px">⏳ {_e(t("web_cron_preview_loading"))}</div>
    </div>
  </div>
  <label>{t("web_excluded")} {help_(t("web_excluded_help"))}{env_("exclude_containers")}</label>
  <input type="text" name="exclude_containers" value="{_e(', '.join(config.exclude_containers))}" placeholder="container1, container2">
  <!-- Web UI password. The stored value is NEVER rendered back into the
       field (it is a secret, not on LOGGABLE_PERSISTENT_KEYS) — the box
       always starts empty and an empty submit means "leave unchanged". -->
  <label>{t("web_password_label")} {help_(t("web_password_hint"))}{env_("web_password")}</label>
  <input type="password" name="web_password" value="" placeholder="{_e(t('web_password_placeholder'))}" autocomplete="new-password">
  <!-- NOT adv-only. It used to be, and that closed the last door: DEBUG
       from the environment can be overruled by settings.json, and the
       only other way to switch debug on was a checkbox that simple mode
       hides with display:none — invisible even to the browser's own
       find-in-page. Env powerless, switch unfindable, no way in at all
       (#53, @LeeNX). One toggle in the simple view is the smaller cost. -->
  <div class="form-checkbox-row">
    <input type="checkbox" name="debug" id="cb-debug" {cb(config.debug)}>
    <label for="cb-debug">{t("web_debug_mode")} {help_(t("web_debug_help"))}{env_("debug")}</label>
  </div>
</div>

<!-- ── Updates ────────────────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="updates">
  <div class="form-checkbox-row">
    <input type="checkbox" name="auto_selfupdate" id="cb-auto-su" {cb(config.auto_selfupdate)}>
    <label for="cb-auto-su">{t("web_auto_selfupdate")} {help_(t("web_auto_selfupdate_help"))}{env_("auto_selfupdate")}</label>
  </div>

  <div class="grid adv-only">
    <div>
      <label>{t("web_healthcheck_max_starting")} {help_(t("web_healthcheck_max_starting_hint"))}{env_("healthcheck_max_starting")}</label>
      <input type="number" name="healthcheck_max_starting" value="{_e(config.healthcheck_max_starting)}" min="30" max="3600">
    </div>
    <div>
      <label>{t("web_docker_stop_timeout")} {help_(t("web_docker_stop_timeout_hint"))}{env_("docker_stop_timeout")}</label>
      <input type="number" name="docker_stop_timeout" value="{_e(config.docker_stop_timeout)}" min="1" max="3600">
    </div>
  </div>
  <p class="form-help">{t("web_updates_tab_hint")}</p>
</div>

<!-- ── Aufräumen ─────────────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="cleanup">
  <div class="form-checkbox-row">
    <input type="checkbox" name="auto_cleanup" id="cb-auto-cl" {cb(config.auto_cleanup)}>
    <label for="cb-auto-cl">{t("web_auto_cleanup")}{env_("auto_cleanup")}</label>
  </div>
  <p class="form-help">{t("web_auto_cleanup_hint")}</p>

  <div class="grid adv-only">
    <div>
      <label>{t("web_cleanup_grace_hours")} {help_(t("web_cleanup_grace_hours_hint"))}{env_("cleanup_grace_hours")}</label>
      <input type="number" name="cleanup_grace_hours" value="{_e(config.cleanup_grace_hours)}" min="0" max="8760">
    </div>
    <div>
      <label>{t("web_cleanup_backup_days")} {help_(t("web_cleanup_backup_days_hint"))}{env_("cleanup_backup_days")}</label>
      <input type="number" name="cleanup_backup_days" value="{_e(config.cleanup_backup_days)}" min="1" max="365">
    </div>
  </div>
  <div class="form-checkbox-row adv-only">
    <input type="checkbox" name="cleanup_backup_local_only" id="cb-bak-local" {cb(config.cleanup_backup_local_only)}>
    <label for="cb-bak-local">{t("web_cleanup_backup_local_only")}{env_("cleanup_backup_local_only")}</label>
  </div>
  <p class="form-help adv-only">{t("web_cleanup_backup_local_only_hint")}</p>
</div>

<!-- ── Benachrichtigungen ────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="notifs">
  <div class="grid adv-only">
    <div>
      <label>{t("web_disk_warn_percent")} {help_(t("web_disk_warn_percent_hint"))}{env_("disk_warn_percent")}</label>
      <input type="number" name="disk_warn_percent" value="{_e(config.disk_warn_percent)}" min="50" max="100">
    </div>
    <div>
      <div class="form-checkbox-row" style="margin-top:24px">
        <input type="checkbox" name="disk_warn_auto_cleanup" id="cb-disk-acl" {cb(config.disk_warn_auto_cleanup)}>
        <label for="cb-disk-acl">{t("web_disk_warn_auto_cleanup")}{env_("disk_warn_auto_cleanup")}</label>
      </div>
      <p class="form-help">{t("web_disk_warn_auto_cleanup_hint")}</p>
    </div>
  </div>

  <hr class="section-divider adv-only">

  <div class="grid">
    <div>
      <label>{t("web_quiet_hours_start")}{env_("quiet_hours_start")}</label>
      <input type="text" name="quiet_hours_start" value="{_e(config.quiet_hours_start)}" placeholder="22:00" pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$|^$">
    </div>
    <div>
      <label>{t("web_quiet_hours_end")}{env_("quiet_hours_end")}</label>
      <input type="text" name="quiet_hours_end" value="{_e(config.quiet_hours_end)}" placeholder="07:00" pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$|^$">
    </div>
  </div>
  <p class="form-help">{t("web_quiet_hours_hint")}</p>

  <hr class="section-divider adv-only">

  <div class="adv-only">
  <h3 style="font-size:14px;color:var(--accent);margin-bottom:8px">{t("web_weekly_title")}</h3>
  <div class="form-checkbox-row">
    <input type="checkbox" name="weekly_report_enabled" id="cb-weekly" {cb(config.weekly_report_enabled)}>
    <label for="cb-weekly">{t("web_weekly_enable")}{env_("weekly_report_enabled")}</label>
  </div>
  <p class="form-help">{t("web_weekly_hint")}</p>
  <div class="grid">
    <div>
      <label>{t("web_weekly_day")}{env_("weekly_report_weekday")}</label>
      <select name="weekly_report_weekday">
        {''.join(f'<option value="{i}" {"selected" if int(config.weekly_report_weekday or 0)==i else ""}>{name}</option>' for i, name in enumerate([t("web_weekday_mon"), t("web_weekday_tue"), t("web_weekday_wed"), t("web_weekday_thu"), t("web_weekday_fri"), t("web_weekday_sat"), t("web_weekday_sun")]))}
      </select>
    </div>
    <div>
      <label>{t("web_weekly_hour")}{env_("weekly_report_hour")}</label>
      <input type="number" name="weekly_report_hour" value="{_e(config.weekly_report_hour)}" min="0" max="23">
    </div>
  </div>
  </div>

  <hr class="section-divider">

  <h3 style="font-size:14px;color:var(--accent);margin-bottom:8px">{t("web_monitor_title")}</h3>
  <div class="form-checkbox-row">
    <input type="checkbox" name="monitor_enabled" id="cb-monitor" {cb(config.monitor_enabled)}>
    <label for="cb-monitor">{t("web_monitor_enabled")} {help_(t("web_monitor_hint"))}{env_("monitor_enabled")}</label>
  </div>
  <div class="grid">
    <div>
      <label>{t("web_monitor_interval")} {help_(t("web_monitor_interval_hint"))}{env_("monitor_interval_seconds")}</label>
      <input type="number" name="monitor_interval_seconds" value="{_e(config.monitor_interval_seconds)}" min="15" max="86400">
    </div>
  </div>
</div>

<!-- ── Kanäle ────────────────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="channels">
  <div class="adv-only">
    <label>Telegram Topic ID {help_(t("web_topic_id_help"))}{env_("telegram_topic_id")}</label>
    <input type="text" name="telegram_topic_id" value="{_e(config.telegram_topic_id)}" placeholder="{_e(t('web_topic_id_placeholder'))}">

    <label>{t("web_allowed_users")} {help_(t("web_allowed_users_help"))}{env_("telegram_allowed_users")}</label>
    <input type="text" name="telegram_allowed_users" value="{_e(', '.join(str(u) for u in (config.telegram_allowed_users or [])))}" placeholder="{_e(t('web_allowed_users_placeholder'))}">

    <label>{t("web_bot_label")} {help_(t("web_bot_label_help"))}{env_("bot_label")}</label>
    <input type="text" name="bot_label" value="{_e(config.bot_label or '')}" placeholder="{_e(t('web_bot_label_placeholder'))}">
  </div>

  <label>Discord Webhook {help_(t("web_discord_help"))}{env_("discord_webhook")}</label>
  <div style="display:flex;gap:8px">
    <input type="text" name="discord_webhook" id="f-discord_webhook" value="{_e(config.discord_webhook)}" placeholder="https://discord.com/api/webhooks/..." style="flex:1">
    <button type="button" class="btn-sm btn-outline" onclick="dsTestWebhook('discord')" title="{_e(t('web_test_send'))}">{t("web_test_send")}</button>
  </div>

  <label>Webhook URL {help_(t("web_webhook_help"))}{env_("webhook_url")}</label>
  <div style="display:flex;gap:8px">
    <input type="text" name="webhook_url" id="f-webhook_url" value="{_e(config.webhook_url)}" placeholder="https://your-service/webhook" style="flex:1">
    <button type="button" class="btn-sm btn-outline" onclick="dsTestWebhook('webhook')" title="{_e(t('web_test_send'))}">{t("web_test_send")}</button>
  </div>
</div>

<div style="margin-top:16px">
  <button type="submit" class="btn">{t("web_save")}</button>
</div>
</form>
</div>

<div class="card" id="groups">
<h2>{t("web_groups_title")}</h2>
<p class="card-intro">{t("web_groups_moved_intro")}</p>
<p style="margin-top:8px"><a href="/groups" class="btn">📦 {t("web_groups_moved_button")}</a></p>
</div>

<div class="card" id="backup">
<h2>{t("web_backup_title")}</h2>
<p class="card-intro">{t("web_backup_intro")}</p>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
  <a href="/api/backup_export" class="btn">⬇ {t("web_backup_export")}</a>
  <button type="button" class="btn btn-outline" onclick="document.getElementById('backup-import-file').click()">⬆ {t("web_backup_import")}</button>
  <input type="file" id="backup-import-file" accept=".json,application/json" style="display:none" onchange="dsBackupImport(this)">
</div>
<p class="form-help" style="margin-top:12px">{t("web_backup_hint")}</p>
</div>

<div class="card adv-only" id="windows">
<h2>{t("web_windows_title")}</h2>
<p class="card-intro">{t("web_windows_intro")}</p>
{self._windows_html(t)}
</div>

<div class="card">
<h2>{t("web_maint_mode_title")}</h2>
<p class="card-intro">{t("web_maint_mode_intro")}</p>
{self._maint_mode_html(t)}
</div>

<div class="card">
<h2>{t("web_maintenance_title")}</h2>
<p class="card-intro">{t("web_maintenance_intro")}</p>
<form method="POST" action="/api/cleanup" style="display:inline;margin-right:8px"
      data-confirm="{_e(t('web_confirm_cleanup'))}"
      data-confirm-title="{_e(t('web_maintenance_cleanup'))}"
      data-confirm-label="{_e(t('web_confirm_cleanup_btn'))}">
<button type="submit" class="btn btn-blue btn-icon-text">{_ICONS["broom"]}<span>{t("web_maintenance_cleanup")}</span></button>
</form>
<form method="POST" action="/api/selfupdate" style="display:inline"
      data-confirm="{_e(t('web_confirm_selfupdate'))}"
      data-confirm-title="{_e(t('web_maintenance_selfupdate'))}"
      data-confirm-label="{_e(t('web_confirm_selfupdate_btn'))}"
      data-confirm-danger="1">
<button type="submit" class="btn btn-icon-text">{_ICONS["arrow_up"]}<span>{t("web_maintenance_selfupdate")}</span></button>
</form>
<p class="form-help" style="margin-top:12px">
{t("web_maintenance_explain", grace=_e(config.cleanup_grace_hours), days=_e(config.cleanup_backup_days), dir=_e(config.cleanup_backup_dir))}
</p>
</div>

<div class="card">
<h2>Info</h2>
<table>
<tr><td>Version</td><td><code>v{_e(VERSION)}</code></td></tr>
<tr><td>Telegram</td><td><code>{telegram_status}</code></td></tr>
<tr><td>Bot Token</td><td><code>{_e(token_masked)}</code></td></tr>
<tr><td>Chat ID</td><td><code>{_e(chat_masked)}</code></td></tr>
<tr><td>Data Dir</td><td><code>{_e(config.data_dir)}</code></td></tr>
</table>
<p class="form-help" style="margin-top:8px">{t("web_info_credentials_hint")}</p>
</div>"""

            self._send_html(self._render_page(content, "settings"))

        def _maint_mode_html(self, t):
            """Render the Maintenance-Mode quick-buttons + status."""
            from maintenance import get_state as _ms, format_remaining as _mr
            state = _ms(config)
            if state.get("active"):
                if state.get("until_iso") == "forever":
                    until_text = t("web_maint_forever")
                else:
                    until_text = t("web_maint_until", remaining=_mr(state))
                return f"""<div style="margin-bottom:12px;color:var(--warn)">
<strong>{t("web_maint_active")}</strong> — {until_text}
</div>
<form method="POST" action="/api/maintenance" style="display:inline">
<input type="hidden" name="action" value="off">
<button type="submit" class="btn btn-icon-text">{_ICONS["x"]}<span>{t("web_maint_disable")}</span></button>
</form>"""

            return f"""<div style="display:flex;gap:6px;flex-wrap:wrap">
<form method="POST" action="/api/maintenance" class="inline-form">
<input type="hidden" name="action" value="on">
<input type="hidden" name="hours" value="1">
<button type="submit" class="btn btn-outline btn-sm">{t("web_maint_btn_1h")}</button>
</form>
<form method="POST" action="/api/maintenance" class="inline-form">
<input type="hidden" name="action" value="on">
<input type="hidden" name="hours" value="4">
<button type="submit" class="btn btn-outline btn-sm">{t("web_maint_btn_4h")}</button>
</form>
<form method="POST" action="/api/maintenance" class="inline-form">
<input type="hidden" name="action" value="on">
<input type="hidden" name="hours" value="24">
<button type="submit" class="btn btn-outline btn-sm">{t("web_maint_btn_1d")}</button>
</form>
<form method="POST" action="/api/maintenance" class="inline-form">
<input type="hidden" name="action" value="forever">
<button type="submit" class="btn btn-outline btn-sm">{t("web_maint_btn_forever")}</button>
</form>
</div>"""

        # _groups_html removed in v1.22.0 — dead code since v1.21.1 when
        # the legacy Settings → Groups card was replaced by a redirect
        # banner pointing at /groups. All Container Groups functionality
        # lives in _page_groups now.

        def _windows_html(self, t):
            """Render the Update Windows table + add-form for the Settings page."""
            try:
                containers = self._get_containers()
            except Exception:
                containers = []
            container_names = sorted({c["name"] for c in containers})
            current = store.get_update_windows() or {}

            wd_short = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
            rows_html = ""
            for name in sorted(current.keys()):
                w = current[name]
                days_set = set(w.get("weekdays") or [])
                days = "·".join(wd_short[i] if i in days_set else " " for i in range(7))
                rows_html += f"""<tr>
<td><code>{_e(name)}</code></td>
<td><code>{_e(w.get('start',''))}–{_e(w.get('end',''))}</code></td>
<td><code>{_e(days if days_set else 'all days')}</code></td>
<td>
<form method="POST" action="/api/window" style="display:inline">
<input type="hidden" name="name" value="{_e(name)}">
<input type="hidden" name="action" value="delete">
<button type="submit" class="btn-sm btn-outline">{t("web_delete")}</button>
</form>
</td>
</tr>"""
            if not rows_html:
                rows_html = (f"<tr><td colspan=\"4\" style=\"color:#8b949e;font-size:12px\">"
                             f"{t('web_windows_empty')}</td></tr>")

            options = "".join(f'<option value="{_e(n)}">{_e(n)}</option>'
                              for n in container_names)
            wd_full = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            wd_html = ""
            for i, label in enumerate(wd_full):
                wd_html += (f'<label style="display:inline-block;margin-right:10px;font-size:13px">'
                            f'<input type="checkbox" name="weekdays" value="{i}" '
                            f'style="width:auto;margin-right:4px">{label}</label>')

            return f"""<table style="margin-bottom:14px">
<tr><th>{t("web_name")}</th><th>{t("web_windows_range")}</th><th>{t("web_windows_days")}</th><th>{t("web_actions")}</th></tr>
{rows_html}
</table>
<form method="POST" action="/api/window">
<input type="hidden" name="action" value="save">
<div class="grid">
<div>
<label>{t("web_windows_container")}</label>
<select name="name">{options}</select>
</div>
<div>
<label>{t("web_windows_range")}</label>
<div style="display:flex;gap:8px">
<input type="text" name="start" placeholder="02:00" pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$" required>
<input type="text" name="end" placeholder="04:00" pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$" required>
</div>
</div>
</div>
<div style="margin-top:8px">
<label>{t("web_windows_days")}</label>
{wd_html}
<p style="font-size:11px;color:#484f58;margin:4px 0 0 0">{t("web_windows_days_hint")}</p>
</div>
<button type="submit" class="btn" style="margin-top:8px">{t("web_windows_save")}</button>
</form>"""

        def _page_logs(self):
            t = _web_translator(config.language)

            query = parse_qs(urlparse(self.path).query)
            container = query.get("container", [""])[0]
            lines = int(query.get("lines", ["50"])[0])

            containers = self._get_containers()

            # Container dropdown (escape names — they appear in HTML attribute and content)
            options = ""
            for c in containers:
                sel = 'selected' if c["name"] == container else ''
                name_e = _e(c["name"])
                options += f'<option value="{name_e}" {sel}>{name_e}</option>\n'

            log_html = ""
            if container:
                result = backend.logs(container, tail=lines, timeout=10)
                output = result.stdout or result.stderr
                if output.strip():
                    log_html = f'<pre>{html.escape(output.strip())}</pre>'
                else:
                    log_html = '<p style="color:#8b949e">No logs found.</p>'

            content = f"""
<div class="card">
<h2>{t("web_logs")}</h2>
<form method="GET" action="/logs" class="logs-filter">
<div class="logs-filter-grow">
<label>Container</label>
<select name="container">{options}</select>
</div>
<div class="logs-filter-lines">
<label>{t("web_logs_lines")}</label>
<input type="number" name="lines" value="{lines}" min="10" max="500">
</div>
<button type="submit" class="btn btn-blue">{t("web_logs_show")}</button>
</form>
{log_html}
</div>"""

            self._send_html(self._render_page(content, "logs"))

        def _run_web_update_batch(self, keys):
            """Run the pending updates for `keys` through the SAME engine as
            every other update flow (v1.60.5). Web UI updates used to bypass
            the shared update core: they called checker.update_container in a
            bare loop, so a Web-triggered update skipped the group ordering,
            the netns-owner snapshot, the restart-dependents cascade and the
            per-container cooldown that the Telegram "Update all" path gets —
            and the bulk path didn't even take the update lock, so it could
            collide with a running batch or a self-update swap. Routing both
            Web handlers through engine._process_update_batch under the shared
            lock closes that divergence; the notifier fan-out and Telegram
            summary now match the bot path exactly.

            `keys` are host keys (#7) — bare names for the local host,
            `nas/nginx` for a remote one. Each host's entries come from ITS
            slice of the pending file and are updated through ITS checker;
            matching a remote host's entry by bare name would recreate the
            local container from the remote one's image, which is exactly
            the accident this indirection exists to prevent.

            There is still exactly ONE lock, held across every host: two
            hosts' recreates may be independent, but the pending file, the
            history file and the self-update swap they all touch are not.
            """
            from container_store import split_host_key
            engine = bot.engine
            # host → the names asked for on it, in first-seen order so a
            # single-host batch keeps the order it always had.
            wanted, order = {}, []
            for key in keys:
                host_name, name = split_host_key(key)
                if not name:
                    continue
                if host_name not in wanted:
                    wanted[host_name] = set()
                    order.append(host_name)
                wanted[host_name].add(name)

            batches = []          # (host, checker, [pending entries])
            targets = []
            for host_name in order:
                resolved = _resolve_host(host_name)
                if resolved is None:
                    # A host we don't manage. Skipped in full — never
                    # retried against the local one.
                    print(f"Web UI update: unknown host {host_name!r} — skipped")
                    continue
                hname, _be, hchecker, _st = resolved
                entries = [u for u in self._get_pending(hname)
                           if u.get("name") in wanted[host_name]]
                if entries:
                    batches.append((hname, hchecker, entries))
                    targets.extend(entries)
            if not targets:
                return
            # Atomic claim of the shared update mutex — same one the bot's
            # run_updates / single-update / self-update flows use. Busy → bail.
            if not engine._update_lock.acquire(blocking=False):
                if bot.enabled:
                    bot.send_message(bot.t("update_already_running"))
                return
            try:
                # All per-container work (enrich, group-order sort, netns
                # snapshot, update, cascade, cooldown, notifier results) runs
                # in the shared engine. auto=False: no ask-before-major gate —
                # clicking Update in the Web UI is the explicit "do it now".
                # One call per host: the engine resolves per-host STATE from
                # each entry's `host` key, but the checker that actually
                # recreates a container is a single parameter, so a batch
                # must not mix hosts.
                results = []
                for hname, hchecker, entries in batches:
                    r, _sc, _mp = engine._process_update_batch(
                        entries, hchecker, auto=False)
                    results.extend(r)
                # Drop the processed containers from pending (atomic); the file
                # may hold others this action didn't touch — including another
                # host's entry for a container of the same name, hence the
                # (host, name) pairs.
                bot._remove_from_pending(
                    [(u.get("host") or _LOCAL_HOST, u["name"]) for u in targets])
                if bot.enabled:
                    bot.send_message(bot.t("update_result") + "\n\n" + "\n".join(results))
            except Exception as e:
                print(f"Web UI update error: {e}")
            finally:
                engine._update_lock.release()
                bot._run_queued_selfupdate()

        def _api_update(self, key):
            """Trigger update for a single container from Web UI. Runs through
            the shared engine batch under the update lock (v1.60.5). `key` is
            the host key the row's form carried."""
            self._run_web_update_batch([key])

        def _api_check(self):
            # The race guard the Telegram /check branch has had since #26,
            # which this path never got: a check running alongside an update
            # still sees the pre-pull digest and reports a phantom
            # "update available" a few seconds after the user hit Update.
            if bot.update_running:
                print("Web UI check skipped: an update is currently running")
                return
            # …and one against the Web UI's own double-click: two clicks used
            # to start two checks that both wrote config.pending_file (#50).
            if not _CHECK_LOCK.acquire(blocking=False):
                print("Web UI check skipped: a check is already running")
                return
            try:
                # No `bot=` here: with DEBUG on, check_all pushes its whole
                # debug log to Telegram for the requester — right for the
                # Telegram /check command, spam for a Web UI click (the log
                # is on the /logs page anyway). Found-updates notifications
                # below are unaffected. (#35 feedback, @NotRetarded)
                updates = checker.check_all()
                if updates:
                    bot.notify_updates(updates)
            except Exception as e:
                print(f"Web UI check error: {e}")
            finally:
                _CHECK_LOCK.release()

        def _api_check_one(self, name, hchecker=None):
            """Check a single container and return a JSON-able result dict.

            Runs inline instead of in a thread: one registry HEAD request is
            quick enough to wait for, and the caller needs the outcome. The
            old global check fired a thread and redirected immediately, so
            the user saw the stale numbers and got feedback only via
            bot.notify_updates — which does nothing on an install without
            Telegram, Discord or a webhook. That's the whole complaint in #50.

            `hchecker` is the checker of the host the row belongs to (#7);
            it defaults to the local one, which is the only checker a
            single-host install has. `check_all` stamps its results with
            that checker's host and merges them into the pending file
            per host, so a scoped remote check can't wipe local entries.
            """
            hchecker = hchecker if hchecker is not None else checker
            if not name:
                return {"ok": False, "error": "missing name"}
            if bot.update_running:
                return {"ok": False, "busy": True, "error": "update running"}
            if not _CHECK_LOCK.acquire(blocking=False):
                return {"ok": False, "busy": True, "error": "check running"}
            try:
                own_name = ""
                try:
                    # Only meaningful for the local host — this process runs
                    # nowhere else, so a remote container of the same name is
                    # an ordinary container, not us.
                    if hchecker is checker:
                        own_name = checker._own_container_name()
                except Exception:
                    pass
                if own_name and name == own_name:
                    # get_running_containers filters our own container out,
                    # so check_all(only={us}) can only ever come back empty
                    # and the button would look broken on that one row. Ask
                    # the registry directly instead — digest compare, no pull.
                    checker.debug_log = []
                    found = checker.has_selfupdate_available()
                    result = {"ok": True, "name": name,
                              "found": bool(found), "selfupdate": True}
                else:
                    updates = hchecker.check_all(only={name})
                    result = {"ok": True, "name": name,
                              "found": any(u.get("name") == name for u in updates),
                              "selfupdate": False}
            except Exception as e:
                print(f"Web UI single check error: {e}")
                return {"ok": False, "error": str(e)[:200]}
            finally:
                _CHECK_LOCK.release()
            # Debug lines only when DEBUG is on. The buffer is empty
            # otherwise, so this changes nothing functionally — but the log
            # carries registry hosts and repository paths, and on a private
            # registry that's not something to hand out by default.
            if config.debug:
                result["debug"] = list(getattr(hchecker, "debug_log", ()) or ())
            return result

        def _api_cleanup(self):
            """Run `docker image prune` to free disk space (manual trigger)."""
            try:
                ok, msg = bot.cleanup_guarded(checker)
                if ok is None:
                    bot.send_message(msg)
                    print(f"Cleanup skipped: {msg}")
                    return
                if bot.enabled:
                    bot.send_message(f"{'✅' if ok else '❌'} {msg}")
                if bot.notifier and bot.notifier.has_channels():
                    bot.notifier.send_message(f"🧹 Cleanup: {msg}")
                print(f"Cleanup: {msg}")
            except Exception as e:
                print(f"Web UI cleanup error: {e}")

        def _api_selfupdate(self):
            """Trigger a self-update of the Docksentry container."""
            try:
                # The TelegramBot class owns the selfupdate logic regardless
                # of whether Telegram itself is configured — when disabled,
                # internal send_message() calls are no-ops and the Discord/
                # webhook channels (via notifier) carry the status messages.
                if bot.enabled:
                    bot._handle_selfupdate()
                else:
                    # Headless variant — reuse the auto-selfupdate path,
                    # which now prints its outcome (found / up-to-date /
                    # pull failed / busy) to the container log; on headless
                    # installs that log is the user's only feedback channel
                    # (#43, @LeeNX pressed this button and got silence).
                    applied = bot.check_selfupdate_auto()
                    print(f"Web UI selfupdate (headless): "
                          f"{'update started' if applied else 'no update applied — see lines above for the reason'}")
            except Exception as e:
                print(f"Web UI selfupdate error: {e}")
                if bot.notifier and bot.notifier.has_channels():
                    bot.notifier.send_message(f"❌ Selfupdate failed: {e}")

        def _api_bulk(self, action, keys):
            """Apply a bulk action to a list of containers.

            Supported actions: pin, unpin, autoupdate_on, autoupdate_off,
            update. Update walks through the pending-updates list and runs
            each matching update sequentially.

            `keys` are host keys (#7) — the checkbox values from the status
            table. A selection may span hosts, so the names are grouped by
            host first and each group is applied through that host's own
            store view. A key naming a host we don't manage is dropped, not
            retried locally.
            """
            from container_store import split_host_key
            by_host = {}
            for key in keys:
                host_name, name = split_host_key(key)
                if name:
                    by_host.setdefault(host_name, []).append(name)
            try:
                if action == "update":
                    # Route through the shared engine batch under the update
                    # lock — same path as single-update and the bot (v1.60.5),
                    # so bulk gets group ordering, netns, cascade, cooldown and
                    # mutex protection instead of a bare update_container loop.
                    # It does its own host resolution, on the raw keys.
                    self._run_web_update_batch(keys)
                    return
                if action not in ("pin", "unpin", "autoupdate_on",
                                  "autoupdate_off"):
                    print(f"Web UI bulk: unknown action {action!r}")
                    return
                for host_name, names in by_host.items():
                    resolved = _resolve_host(host_name)
                    if resolved is None:
                        print(f"Web UI bulk: unknown host {host_name!r} — skipped")
                        continue
                    hname, _be, _ck, hstore = resolved
                    if action == "pin":
                        for n in names:
                            hstore.pin(n)
                    elif action == "unpin":
                        for n in names:
                            hstore.unpin(n)
                    elif action == "autoupdate_on":
                        auto = hstore.get_autoupdate()
                        for n in names:
                            # Same self-guard as /api/autoupdate (#51): our own
                            # name in the opt-in list does nothing and gets wiped
                            # on the next boot. Local host only — see there.
                            if n in auto:
                                continue
                            if hname == _LOCAL_HOST and self._is_own_container(n):
                                continue
                            auto.append(n)
                        hstore.save_autoupdate(auto)
                    elif action == "autoupdate_off":
                        auto = hstore.get_autoupdate()
                        auto = [a for a in auto if a not in names]
                        hstore.save_autoupdate(auto)
            except Exception as e:
                print(f"Web UI bulk error: {e}")

    return WebHandler


class WebUI:
    def __init__(self, config, checker, bot, store, port=8080, password="",
                 backend=None, hosts=None):
        self.config = config
        self.port = port
        self.handler = create_handler(config, checker, bot, store,
                                      password or None, backend, hosts)
        self.server = None
        self.thread = None

    def start(self):
        self.server = ThreadingHTTPServer(("0.0.0.0", self.port), self.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        print(f"Web UI started on port {self.port}")

    def stop(self):
        if self.server:
            self.server.shutdown()
