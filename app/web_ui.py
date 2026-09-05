#!/usr/bin/env python3
"""Optional lightweight Web UI for configuration and status."""

import base64
import hashlib
import hmac
import html
import io as _io
import ipaddress
import json
import os
import subprocess
import sys
import threading
import time

import webauth
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, urlparse

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
                 ".svg": "image/svg+xml; charset=utf-8",
                 # No charset on image/png — it is binary, and a charset
                 # parameter on a binary type makes some proxies try to
                 # transcode it.
                 ".png": "image/png"}
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
#: Action-button glyphs. Emoji, not line art — and that was a decision,
#: not a shortcut. The legend under the container table exists BECAUSE the
#: previous SVG set needed explaining, which is the argument against it,
#: made by us. A 📌 does not need a legend. @NotRetarded asked for the
#: change and picked most of these (#2); the trade accepted with it is that
#: emoji render differently per platform and cannot be recoloured, so the
#: table is louder than it was.
#:
#: One departure from his list: he suggested 🔊 for major-confirm, which
#: reads as "sound". ⚠️ says what it means.
_ICONS = {
    "refresh":   '<span class="ic">🔃</span>',
    "restart":   '<span class="ic">♻️</span>',
    "pin":       '<span class="ic">📌</span>',
    "settings":  '<span class="ic">🔁</span>',
    "alert":     '<span class="ic">⚠️</span>',
    # The major-confirm TOGGLE, not a warning. ⚠️ sat on every row —
    # the button is always there and only its highlight carries state,
    # so a full table read as "all my containers have an alert" (#2,
    # @LeeNX, with a screenshot that makes the point better than any
    # argument). ❓ says what the button does: ask me before a major
    # jump. ⚠️ stays where a major update genuinely IS waiting.
    "ask":       '<span class="ic">❓</span>',
    "checkmark": '<span class="ic">✅</span>',
    "x":         '<span class="ic">🛑</span>',
    "search":    '<span class="ic">🔎</span>',
    # Tab glyphs — these were never the confusing ones, but the set has to
    # be complete or the tabs render an empty span.
    "broom":     '<span class="ic">🧹</span>',
    "calendar":  '<span class="ic">🗓️</span>',
    "package":   '<span class="ic">📦</span>',
    "arrow_up":  '<span class="ic">⬆️</span>',
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


def _metric_label(value):
    """Escape a Prometheus label value.

    Backslash, double quote and newline are the three the exposition format
    requires escaping — and a container name is user-controlled text, so an
    unescaped quote would produce a malformed line that costs the scraper
    the whole response, not just that metric.
    """
    return (str(value or "")
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", ""))


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


def _updating_label(t, version):
    """Badge text while a container's update is actually running.

    The yellow "update" badge means *available*, and it kept saying that
    while the log already said the update was under way (#2, @LeeNX). One
    helper so the table, the container page and the V2 list cannot word it
    three ways. The target version is best-effort — it comes from the
    remote image's OCI version label — so there is a wording for "we know
    where this is going" and one for "we don't".
    """
    return (t("web_badge_updating", version=version) if version
            else t("web_badge_updating_now"))


def _icon_label(icon_key, label):
    """Return an inline SVG icon followed by a label, both inside a span."""
    return (f'<span class="icon-label">'
            f'{_ICONS.get(icon_key, "")}{label}</span>')


# The brand tile for the header, inlined so the logo paints with the first
# byte of HTML instead of arriving one round trip later and shifting the
# header. It is app/static/icon.png resampled to 96px, not the 256px master:
# the header box is 36 CSS px, so 96 still oversamples it by 2.7× (sharp on
# every retina class in circulation) while the 256px original would have put
# 122 KB of base64 on *every* page response for detail no one can see at
# that size. The full-resolution file is still served — as the favicon, the
# apple-touch-icon and the manifest icon — where it is fetched once and
# cached, and where the display size actually warrants it.
#: Session cookie name. `ds_` prefixed so it cannot collide with a
#: cookie from another app behind the same reverse proxy path.
SESSION_COOKIE = "ds_session"

_LOGO_B64 = "iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAYAAADimHc4AAA9v0lEQVR42rW9ebhlVXnu+/vGmHOuZrdVRXVQUEDRFQUiKCgoYod9GxtQMdgcozHnmKjpzE1uSDw3JkdN1OR4TGKORiQQm2hEgUIEaaUtGhWkqQ6qh6ra/ermGN/5Y4zZrF2lyb3Pc4tnF7vWXnuuOUfzNe/3fu8Q/nN/DKDxC9asaS3pmpd6kXd41WUivEpEUPUYk6CqiMjQBcQIaPEeg/ceMYJoeJ9I/EsF1IMR1IBBUKfxDYTrSnE3CmLCr6kixJ8VP4LyvcX9iBFUw4vhM4mfqZjEomh5rfI+w7tRHIIBlfA8cJ33+X6svTLtj964Y8edneJx45f/jwZW/hODbwEHML58zStF7Os9+iqDrAsDEm8yPqCqxsEODy0iqPeIMdWHiqBxLg8ZNImvUQ2OlD8PbwjXDIOkVJMtYlBVjEi4umrxgeE9GgfXmnBx0fI+1Yf7NiZcQ4vfjfdbTKYWQ2rCNUQorrtZDNfh9erd236+MbzpbRa+6f6/TkDxM51YcczxePl7DC8XY+IHeq8qaoygqrYc2GqxxoGW6kJoWNGAimKweDxGJAymEheNqXaNr+5E4igUg1us/GqAwyRAMYDVwFF+RrigGBN2Y7X9wjo3Ju7zsEQk/lx9nGhRoNh13sX7ESPWmHhN9XqD83xw35M/3VIfx1+2un/l6+Or1v4mKv8mRk5R8Kg6DcNqRNQQDYWitemURYMGqA+DI1rOrkHK6SketNi8ccHGsTPVao07W8SEATIGU1v5KOXeCvdUrNJ4UQnmQwFjBJVqgdR3lBb3YeJgFwui2IlhSA0iJnyC96reqYK1yTpj5P0jS1ZOzR3ce2/5uIeZBPklK18BJlcceyXGXKzqUMWJiA03EE1OuXLi33HlVLakGl7FhwEv7arUZqdYoVqucCOyyAxUl68Pamn6CqPmFcEgBjyKqZxQuTtQDfMR786IwRPuqdplCj5MAKLgFUxhxuKiqO0m1KNx1YjgVMXaJMOru2r35gffsXhsf9kEGEBHVh59aqLmKyLmbFWfK1hEpVx1cQJK8xLtfGHIpXhgY0s7rCimNA+eMJdafaxoWOR1HzB0xzK0M8LyM+XrPhrnwv8EYxauKcZEy2HiLxfbUzHhA1FT2XpVwBfmRuMOA8XHwY/Bg1SevjS9xUoJkYizSZJ47+7xA/PevU89+PBi52wONxmJyhXGJGd7dQPFJ0LYk+p9WJ9xfxZ2F0+McIrVI/EmY/QRba2qjxGGiQ8mmDLGqGxt3SyLhHdLHAyJC5HC0RbmgegvfOWwq88P+0Odi9fWcL24EzReRJ3iXVjtqr6cfF96XokbuzJvxaIzhR+itJuCkLhBfyDGnG0Sf8XhxtosWv1MrjzuCmOSM7x3A2NMaoPVwRRb2Iff9YWTMxbRMJDGa+nsfHxoNPxuYT/ibyOq8RqKFJ5WhLBJikkGp76M6owKHkU1rMBgy2smJdr2muEa+r50TSql8w6fFXep98GEmXgf0RRWdl9rMx4jKrHRN/jaoiyuqYgxqXduYKw9Y9Vxp12xeNxtPdQcX378h4yxf+h9noOkohKHrLBtUosawtqV4uYFjDEx9DQggo1Rgy8GWSSaDWqrRcrBic49+Ilo5oqQUkTAxPg73ksRLpp65FOLYEunXTjhYlK02k318BkjxKiuctx+kU8rxl9s3Ekm5hXRtizKR+L1raK5TZIzxiZX7J09uPfuEKI+rIV39hMr1x6LmoeNkHl1xoiVYpWE59Kak6wGzNR/LqYevIG66Kwot6whmhlRjDEgJt50YQpinF6+VuUGOhStxAXsCptUNxu2TLRUY7QTF3BY2TL8vVDubLFaLmAxUovTwuAaUzybVLOsMnSfVTwSI794dyAekb56c+re7Q9uq7IJQLx+xYi0om0upzQsUK3MXflNiGrKxKiInOtJjJjwfiHa8fBeY6S04WX4KmDwYSx97TqlL4wrs1itMRrBFg9YLAyDkfrGkuhAQ+5RRnHBkZRmksIneMqdZwqD6Sl3oXoNXyHex3tfBQcoXl2ZpIcvLfyRoIoxpmWN/0pM1MJoLjtq3UvwcqP3ziHBLFU3ZkobrbXMMjxyeFhPiBBEQDxoXPUSs8oQrlX+oTQSRTQVJ0nKODPG9WXGWYS+dfMVTZDUMh1dbP1rg1z7PSmiNWNi3G/KBFDqO7g0i6a6RlzqFdyitQRU4n3HPEJrSVt5XXXGptahL927+cGbDIDL/cUqqJowikU4FlZsjG+Jq8LG9CliMKUvCOhJHDDFlN4jmpqQs1TvEwFjwnzXnJaJ8bUU5s+7sAQLmx9NTfH/sFsoEyhT+JZiG5jhTLqEFEyVAxTDrGiM5anlN1JBGsXAVrY97sx6KidlKB1StDh1FZblQdV4fzGArFmzpjWXpw8bY45V9SoqgqnCK5XK+VM4vCLCKR1chA9UY2YZY3pjEe+infcVplJDyopQNkASIZUP+UCIRMpAwJiavdZaRh3C45DoWlS0jOXLh3fBdofgINxueJ+UIIEYKTPq+i5XT+0+KidfvF5FIILE5y8Dh9I/aG1sRMWIiLLNDA6emswN0peJyFr1zosUAZWWuImIDealSOWpOczSOZpaiFnLT6JTLDM8VcRYvLpqVRWIZrS1hcEwYsF7xBT+TvGuD96Tq6kiI5uAJOigi9EuSZZhTAYCLn5Oae+jcS5MPkNQheJ0EHOUCHcQJ6yWTErcHWWmLvVFKENIrJcqEJYyCdS4hMxan068LFH824yxoipeIwZiajdHLfYIN1B5/CKtl1qMXdh0jcmLDlthVF01XUUkqhqCkALoIqx+9Yob9PAKzjbRkSNJV55M87hzcM3xYLLGlmEbLeTpx+g+cQf9vU+g07uxroexFmMSjLWoVg63XFxVnBXNRB2xjX5Ma7arevAAc6jUJqcIVqQIjCp8yxN3SIG2q4pR45y8LbFiV0o0K2VCUq50W8XmVIDWEOxgQqxc4ujelGmGVw3bsgQtIqZSrgOPEYuvw2fe4/IeuXN4aZIsO4nk6DNoHPs87OoNuLEVDJI2ziSoKN4LDsfEuuczfs7bMNN7yHc/Qu/R2+ltf5D+/u0wP0WSWGzWREwSohWvhbuqEodioACcx0t0yzpshkvTZ3wc0WjrQ6BZw8NMGJ+4mKrdUWBobqUsXbWush1Ek6PKUKbNIg9PFc6F6CXujhruKiX8XMNyKtAAL0WWLKgb4PIBTg3anMQsPZZk9RnYo58Dq07FtY8gNw28d+DzYI5MUkYa3git0WDGjElI04REB6Sdg/DMZnpP3EXn8bsY7H0Cv3AQayBJUiRJq8ioNBemzJDLWoMWuavUnj86W61FTFqC2ogpIJfqj9aKHlIs2CWrj1epmZjKyRKdipaRjtbBsdqykBKN9LUoR8uMNQKL4fvSgSp5v4c3DRhZgTniBOzRzyU9cgN+yVpcuoRcUnKfo95jjGASU6b6wxOgNEeScL9eSzzGJmEymomQDubQZ7bS3XY/nSfupb/7cczcbpLC+Zd5VQQGY6EpAHYxDDUyDGiWz1Nl9UNFpRq4Z4zgvdYMXvz50tXHa+EmRaqQbhFCTgG0h5Ker35W25LUbD0RsDK1iSs/Vz0DyZB1F2LXPh+zbB1+bDV5MkruFPUOry5WqJK4KAWxtlptxgS/ESGE5qitIqz4UalRjMQIzRgka5BaS+J7jCw8zZ5//h3y7ZtIGqOAx2DwBbDtq7GuIJIKJZSY25Z7u7QCMuQ7Qy5UwOu+BGMLNDUpbFUdNq4VXdG6URGH9/V6Sxx0qX143M6qisv7qBiMTWr7UMB7dGI1rRd8iJl0NT7vQq6Qd7A2rHJrDGpM6QAXx99avi4VDEyoJYsPIagXU3kwD/T7uDjUjKzCNybitRTjBR/jP1DUgOsPMIDJGiHVLGJyXYQjFZl8jBjRWo06XjFs/KKYVCUkicYUNORU4eaVRaVllTLdJ8LLxTMVhY6QmjvyQQ/nPJpOkC45BnpT+Pn9kFjEa+kEVaHXG+B8BzGhmF/UjYpgw4iEeN3YYSC3FmFIgWaZCOqjcccUGFSVlZoiOVJwfoC6Puo84gNM7ou9bATNHenkagZe6Ox/Eqs5SZphkxQxtoL8jRne/cWOMUXptFrMFNhSgZmpkBQFjKIIUSuPxoKGwYgEjCPaMhBMkiIqOD/A5X2cZvj2cpKj19FY+Sxk5QZGlq9h/sZP03/8BjLbDri6y/G5Q/MBVvNwUxEKKDLkIsPWiIB6KWDhiDvFooCYCh8qZsfIItDdVBUzCfE1vgDINERimvuwgEy4D1XoLcyz6vx3MnruRex/5G76W++ju/MX9A/shO48WWJJkhRjbQxrY2ZeIMZ1HMswlC1TM+xJmfnFlVH4eq95cE6A92G2jDGgDp8PcN0+Pm2hI6sxx5xKY+05mFWn4cbX4JIW/YHitYfaNknaiNWx8PFJpH8YzWPcFgqWxQ1LmfXWwL4IjkkBdJg6zq8YVYyVaIsrhgu1aMxHZxtygBzyQVgUEm1+dLoSzWRXU9z4WjhnLWPPfyuT3Sl4ejMLWx5g4bG76O9+DL9wgMwabJIFFLdI3HyxK3wtooywh89L45TU+TZF+FkAbd45sMHTuzzHOYdKioysIllzCukx52BWbcCNHU3ftsk9+AWPyFwYMOvJB92w5SQ4eRVwPYfLQbI2mjRQdRVIY03wf8bE3VEBcgg4Ytovi3D+GIFoUSKUeg5SAWtl4qcGtSMszHfJPaRpGla0JIi1JFkLSduIMfQXFhjYBLJJkmPPJVt3HpPnX4JMPUnvsbvpPHo3nScfxs/uI9GcpJFhbBLryYXJlljsMWWEacSQFLwbrZMaNEZFMckaYNClJ9JYfQZ25ano8pPI2yvo0yT3Hj/wmH6nBMMEj8ODH6AL0/TnpjG2gceg7SNIjnwWjbXn0WtMxsIGRGILXipAq47fSFk+lmjvi3hcK1je1OrJWhVuGMpFQiTXE8OKiz7Bsue8jIXNm5h/6ud0nn4S050jS1PU5+BzvIaEzIrinaPrunRVwDSxK06ltfYMll7wLsz+JxlsfZDOY5uY23wfdm4PWZEHSFI6e9G4w2Lgk8gQ0KBDTkOMQf0AmTiZidf8BXONlfTU4nKHuhyLKyOWqlpeIIqh2CLNJbjx40mPOp3GkadjVm9Alx5HnozQ84Ihj9CFoEaq7NRIhLtjGdLIcDXVVKQqMRFF0IoH5GMSJYur3qVZEmYn19I87wTGz30bkzNPM9jxMAuP30138yZ6W39Kf36Ops/D9SSMh8XGkNRjdEDezXHWkqw+mcYxp9J+0Ts4cs8jbP7Ch9HZvWBsCecL1KLFcI9JVZLzMb0OK7ioQzvn8bbJnBlnvtvHxojJiMSLx1AQA1ZLUMoa6Kshe/5vkDZb+NYR9E2T3Ltg2gYuXCOm62WCUg8ECjsuUivCV0y5igrjQWwJgQ9FEkWpRkyEPLRk0HnnWcgHzAuY5jKS9S+lddpLGOvMsnzPFrpY+qrYNEFsWXUJ+VLciaZItvKcXt6nmyTI2BFgsxD4GItqXoOvQyJaoAtJcBI1sMn7qrDtY4XH5xjfx2ijmjuxVTISR0JLimLwOl4NnfFjAiMoV6CHmIIQlVaIpmqk3EhZBix2ovpIC7HUeEY1emCsDdRJYCJFAlihlYUvKO7PSKyEx9BVxOMHHeZzwSQtkuPODPfl+iHyixCqRVATdxcmQA7x+Y0IJjWYvoO8h8eVk18OVG2snToSKa1OUautHrQAGsT7sDIDgDREEyyyVDSv5bwRfhAQddgY4vlaVFOGa2XqVwx+rHJp9fllZmoKOkpxd66suVTPpmAiLl9QDCNJyxbFoxpoVUDVXhSkMDDg8h5WICl8ToRpyt0vAYDzplY7llDPNgLq81BMiiXK2l8FtRPvHEmJWUuFBPp6ea+INkykbRQlyjrRSaqVTIGheyl8Za0axKGYUg0Ak1olqpwgiSAhtvyZKQpFcddhFPUm0BSN1LCzKjs3yLBZI2SmUt6yiZYu0lJqJVKDxutqjWsa6tFiqhzFGINYCT53MeHYFIUjrRXuPUkBPpX4jxHEawlEqHow4I0JgxohWzWhUlox2qTka9ag/nLFag2gKmN7pSx6DNFKRcpdUKFkPvKNAJPERExJG5bUKo1WilfPIB9gvMdYwWPCyjZaq1JVJUup0OTSVARGTaDRhLJrZfZMkWFLBZoWg1vsYAzBX1ADBjER8bGxShZes2JIAmxa48J4HQKVQx3WgCSIJCGbLBeTr9G/qxKjCMHclPRzM4TbSA3iLm3CItNnIhPDa8H1tCSNjFSUZG4/pn8grKap3Qx2/RzXbDGy/lzM6hPoZ236uQtQQ0Q7A6dJSzzeFEWTghBQhIglA08wRksiALG2LRFqLqyFiJS0RmPC6m8khQnVktArKsNmqIiChlJmqUPLRYk5Fp2LDzBxBeCqwnmNjybF1jY1mnO0nRT4TmnyarCtVI0cEoMpTRKyLKMlA2R6J+6JRxhs28Tck/ej3QNYEdzMM8hgHmNSZm44gvbxz2bsjAsYP/m5mOVr6acjdPs5Lu8hXksqfNi1BqlHpzGjLv1aLEVrBBiNgDFawtVSmO04JsZAaiG1NpingjNaUGJUwYXfdy5EgwmRRlhsNV8vp/kqjDOagybllrIaGM9acULKCKOcbDSScCvmjlLBDFIvVWiVwpssoZGlNAfTyLYH6Dz8I/pbN+Hn9kJ/ltTGSVUlMwnSaoWJHByk97Prmf/pDZixpTSPOY2xZ53P2KnnwsrjGJgmg/4Anw8iMSw2deBLRlw1uFKypU2sVooNRHxMDMXLyYgUzcI34iveUlE3iXXsgDUFWqY6T0JRdvM+0t1rnKCiUlmQlEpH6/ESqCNSciWHidZGhqsBKhU4W+f1hIhBUTEkaULLetKpLfQfvYnZR3+Mf+ZxEtehmaaQJJCMDPFb1fmyyC9GyJpNMhTfPUj/5zey72c3YZYcSevEs5g443wmTz4HXbaGBW/pdTpY0QiBVxUQU/CPTJGNF5MS6ZLYkqwmJYnEY0SxBlJAvA84k6+6A6SIRB1453DehUw45E5VGFrSAAsUtdaVEpZDGFy1FbsZ8fFDglfzWjjekCSVSVS5J8OWdEZIk4y29LF7H6L/ixuYf+xmZGoH1oaab45h0HNxG1dOGecw1tJspOUOlDjZJs1oZI2w2haeYeHeq5m95/vYJUcytv55TJ7zGtobXsCCaZD3uyRiK15RjFTEShnnS/HaIlJaQALCTrHGYEVJbUm7qCInXyRgIY8wNgQ1SQk9aDVAvrDRsSlB/SCCRxXKKHFPKmFrhjwgjw67lifUKObBKRGvb8kaCa3BDGy7nd5Pv09v292Y7kHSLCVpNej2Biw//02ka59NnjucWBrtEdJmgrickbEm0w/ewq4bv0270QjbugyfNdDNUbCGZjICquSze5i69RscuPNqJs94CStf9xuYE5/LfH+Ad3mgNhar3kQ+q2gV+xchbsAKsUbDz40pAwdT8mgLB1M0h7iqIC8GERuccIGrh+9rSVlkCYkVxBo014o1VqB8JnbqUCNkalVPFi+oeMrmFJvRbLVoDabQzbfQ+en3cU/eQzKYZSTLYGQM7zzeefJ+l+yEs3BnvxXf74VgYGwMWhZRJVkqjPTmyTdeiW9koWhfBxTrHToaa9ZJSjttouqY3XQt04/dx+qXX8KKV19CPrmShW4HQbGmCg5EKvS1SEdFFGMUG1ueisG3RrBJUdT3JfpfhF1l75qERDUpsryS3SAlq7Ri5CGRtxOTMBmC4iNHMwx2uFEbiykxzY+hbNZq0BxMw0PfZ+GnP8Dt/hkpXRppE5LRsoHDJhaRBJt6Bl4weQ/f7eCMZdCzeBJwDist3Pxc8APOlY10tSp2CZOEmqyJMIWSi6U9Oon3C+z53hc58MAtHP+m97PyvFewkDbpdrqod9HUmJKIVeBANqK1xhisqXyBsQSfUqtplGy5GrUlTI8nKdG5qiUwOp2qKqy+GERqlEWPqK2oGUXoY+IqVBP5QoZGo0FT59HHbmJh0zfRHfeTGE+WNVHJyoQvznFZxMYaJM0wWQPjXGAqJQnW2rAgrAUTohhKrlKdBDoMTxcBc1n59eGaI60m/Z0P87MvfJwlN53HMa95B5Onn0u3PUGn2wu+JrFYUwK1SOzuLHaBsWFSKPokrInQhZTgX+ysLBeaQuEDaoiA1xj3F/SJGJQ7hziHGI3s5+isCm5+2TAdyt7eeZIko63zmMduYOHB7+B2P0RGTtJuBep60WhRGAqtKCvOO9ygQ9afpWUcXXGoJIw0LGnT4JxnrAUzVhCTBNRRpGIhUOxmys8idyVfVFBy50iSJLD1DIwYZXbTD3nwgR8zcfKZrHnFxUw+/+Xko0fQ7ffB51gx2NgVY63BROzH2uDnbFLgR3aICxu6ixSf+0gMC9FfUqZdNa6jECciNqOVxJOibmxC1KMB5SqbsAWDswZJGowkjnTnJjr3fYN82+2k9EizDNEGea+L967sK6jBr2XOb5OUzCbs+PcvYn78b+TOg6TYLIsONkfcAOb2M9puIwjd+Q4u8lkFXzpliasyS7MYDnty50mzjOnpafCesfFxwNBsj+DVsfDIXfz84buYOOlMjn7Nu1h23qthyRF0un1EPUkcfGuoRUHRNFlb9sw5FOOLHVcxHowGdl5S2UutZclahp1hTpISKqiyXlODoV3Ai6yl0chozT9F/4HvMPvIRpL+flrNBNVWJMB67FGnYieOIu8uhK1swsOIDauGzkE6Wx5CVWhOLCGZnAgWxhhs1iAxobabNTLcgRHmHp/BJYbjXnwhyfgS8r6rGvCcA5fTPbCXfQ8/gOQ51lrm5+a48qp/ppEl/NmffZJ77t5Ec3SC8dEx8NAYGaNlDYPtj/DIF/+E0ev+lRNe906OeukbyUdHmJ/vY8VhTVIV/yWw9Jx61OWh7lGn+2hlFmNDDonU+psKNmrIB0JRpmy/jLi7KYG7AqEM1PK0kdHOZ/APXsfMpm9hZp+i2cggSfAu/I41hkHuOeJF70I3vJrO9DRiDZJCo5WSNUMttbnnYR757AfoT+/nhNdcysRL3kq300HF0hgbo9W0eIWJcZj/0UZu/bMP07Atnv+B3yU7aT2DTh4RTcGrJ20kdH92P1d+6O0kMmBuvsuGDet57WteSSNLeNUrL+Sfv3Y5X/ziP/HATx9mbHycRpqh3pG1WmQC+VMP89Df/gnbrv8Wp771fRzxglewYBv0+72wgMrM1+Bcjs8HgVnnNXjmAvIxUjY4Ioak3glSp5gX8LGJTLgCQihIuEROvkkMI6mSPHUbC/dcgd95P1km2GaKd1oBduoD8cA5Fha65L2cXm4xmuLxDDKhnYdw1dsmJmuRNlp0NSHJPb3BACfQ6fSYG1jU5yz4Fjo/H8i8Pc/e/QewB3tof55MTAwlPQ3fpjs3C3gazTbTswu89GUvopElPPnkkyxZsoT3vfdS3vmOi/jS3/8Tf/XZL7Bv39OsWLEc72JklqaMZsLco/dy82X3cOQ5L2H9u/8b7TPOZuCUmd3baY1P0mpP0shSbJoyiJ2Uqi4kriVdv8LCDIsGXmvRUAS74/8T1FrU2vB9ktIcaTPe34W/5a+Z+f4fY3ffR6vVwJg0kNQ0Ni2UfbYxBsm7GJcTGnJcCONCt3XEXQzWZohNsFlGYi1WTAn/WgO2zEALvMZjjcfY2PUSnWRiLY3EYhKDSTOSNCPLGjzr9A0AXP71b3LO817M179+Jc1mk9/57d/ilht/wOtf90r27dmLdx6c4nLHIM9ptUeYnBhhz10/4rqPvIO7/uxj8NSj2IUDjHcOMNGZZdL4+AxJKQwisbtIh8ZYSUIwU5VIpFzlsTTpcvADRHwZ09JoMWq6yKPfY+7ey5GpbYykKZq1I6akZTirw3hb0YWBt6FCZmM27Y0lT8LkGhSX9wKCaQRNM0yzSWIMptHAJCGLSZuCbTchCTiRyRo0mw2cKkkSHKRRT5IlkIRIyHklSVPWrTsOgNn5BX7xiy1c+v7/yr9c9U0+++m/YP36U/jut67g05/9An/6p5+iPdImyTK89xycmkJFOOqoNZyw7ljWrXS8IN3F+vPOYiJN8GJITcqWk9Zyy67tNKRG2iq7VwIY570PJkjjYBWkiJoYReRBOrzL8d7RbiZkBx6id8/X8Ftvo2E90miWXJcyrNRKnkIYJqtm83vJDjxGZ6EXkpzUkHZSkiwhTRKy/ZsxrovRnGz/kzSeehT6HRpJSnNslCyzJOqZNGNMD+Zot0cwVmjNH6D99Da68wsRsnKk1mLbDfo7tuBdzkA9zUbKkslJAA4e2I9JhCOWLuPaa2/krrtexhc+91ne9a6L+b2Pf4RVq1bxoQ9/DOn1yLKUt7z5Dbz5ja/l3OefzZqjVh9GaiMsvpGGJfeurNBp2ZgezX0E65JA7SurE0gU0tCC3aOKdTlpYhjPZ/Gb/o35h75F0ttPo9ECbCzGVw33RqQs62mkODrnGeQ5uVe2XXc5/rorQ36RDwI8qNG2qAcdgOsBwr1f/h/wvz8XdmijGYhisRBiBbTfI+nNo8AVv/ebSJJhJURViQjWJBhR/KBH5pVOd4HJ8RbtdkBVszQLmbR3LD9iGd3BgPe8/7fodru8//3v4d3vejubN29l43U/4NP/41O88IUvKId6anqaX/ziMR5//Al+8eijHLF0Cb/9Ox+hlw/Y/uSTZeipQ7IOWtZZDFLtgDLDrfOifXS4+RwjO2/h6Xt/AHvuo5kmmDSYG2PNkJyMy3N6gwH9fh+Xu7Dis5T2SJsVE5MsmZxkfGKcsYkxJicmybKENMvI0pQkybCJxUa6B8bE7ntitmnJXU4+yHHO0+336HX7zM7MMTc3y0KnS2d2jqmpg+w/OMXU9CwLnQXyfj+0bzda2LSBYkuYZdXqIxEjJNbQHziajQbNZpP/+pE/YGJigre+9c38yR//Hn/0iY+RpSn9QZ+rr76W7119Dffedz87d+yi2+3S6+znLb92MR/96G8zMzvHwWf2o7lj0B+E57BFN2gswxL8RHKIXo3auJoD9m2TDBYO8MyPvoBdOEDabJWyL/3+gO7MDPkgBxGSJGFiYox1xx/LuhPWceIJx7Nu3XGsPeZo1qxZzcoVy5kYn8Amlv+//8zNd9i9ew+7du5ky9bt3H//JjZteoifP7qFXbt2sWvXTo477hhOXX8CzXYbMRaTVN3/zdYI/+0jv8tzzz6Ltcccg01Trr76B/zlpz/Hvfc9iFel3R6h2R5lfMkynt6Xct4Lzg1+ZXqaqenZAIcXkjlaNbEE5ERjRUzrLXTUtBBCnJRIwsDnpOJJmm0QmJ6ZBoXVq5az4QXPZ8Opp7Du+GNZtWoVG07fwDFHH0WWJocdmF6/z9T+KaamZ5iemmF2bo6F+TnmFxaYnp5ldm6B6Zlp5ubmmVvokg8G5IM8yqIFH5OkKY0so9lsMDo6ysToGO3RFmOjI4yPjbPsiGWsWrWSI1ev5MQTjuOCC17Ie9/zLgD27NnLdddtJE0TVD3Pfc6ZLF0yyfxCDxvtdZ7nJJllz+5dfPLPP8U//dOX+P3f/wSf+eu/I2uOMjExGRnjIWfKBwOMMZyy/hQAtm7Zysx8h/HR8RK7LlpYidpz6h2qGohZBUNBtUbCVB+kDYwhiSveaZi1N7/59Vx6yUU8+8xnsXrVqsOsvnm2bt3Krp27eWrnTnbu2MXWrdvYsXMne/bs4+DBg8zOLtAb9CP07FAUlxdkKi0FP6Su+VVUlLSSFfCxC79CIy1JmtJutVgyOcGKFcs46qijWH/KSTzvec/lrLOezXve8+vlvR511GpO33Ai115zDctXHo2LkjYqkLVHuO2Ou/nND3+ML33pH1m2fCVGBDfI8UXvAsJst8PkxASnbTgVgFtvu51+t4+MF5349Xq4oGKwmoQ6y+Sq48scuWoy88Gxxgf1CEl7Cdqdp9ftctcdN3D6aevLh9j+5FPcfvtPuPnm23j8iS08tWMHU9MzLCx06HV7EY8xYeU2GpFBYAEfFFWi7Bi12rTWiALV97V22aL9syQGVIyDsDIdufPkec5gMMC5HGOEpUsmWX/yCbzspS/lta99Nc9+9mls37aNM59zNt2+pd1ul4pdXsME93t9xkZH8bGRo4gOPQF6fnrPTj70mx/kf33xb+h0e7zwxS/nwYceZmJyEozFmCLc1rJD03uPy7vI5OrjdbhpWmpiGAEJdS4naS/F+D4H9+zkH778v3jfpZfQHwz4jQ99lI3XbWRmbp5BnpMmKWmahk7FJKkG1gi9/oC5uTlQxYplbHw0NsINUw4Xq2TV9SAOlfuSWvG7TsKV0mQVvEynSj7I6SzM0+v2WDI5wdnPPYtL3nURu3fv5i8+9VlM2igFoYpoy5TN5VUPhcbBn11Y4IjJEW656XrWHns0111/I6993RsZGZ/EpCnWJgE9jbBI0R3kXUB7k2FlvUV9szVpPXX94JCBTfc9wAfedymZCFu3beHpp59h5apVsQtQS1uh6nEuKJP0FwYcddRKfvfTf87k+DiXf/1Krrn2BsbHJ+IkmBoQWGvrWcwHXcSpK2uvEeUsBZUoYIAqzlMBm1gmxieRCRgMcm6+7Sdc/8ObWHvMkbRHR1no9EoNuAKtDfJJDOlFWGtx6kkMXP61f2LtsccA8Lkv/B1YS5Kli3oTCn0SLaU4vSqJxC3la8TWQGHySOQ1CgQqR9Yibba5//6H6Pb6NBsZLz7/hdxx6x0hsyu642uifKXWju/zub/5FBe+/GUAvP4Nr+EVr3gDd959P6OjozWlk5qOYo3LJBy6E4Z4TqI1ziG19riqfyuWGoJuRTQzk+MTMLGEmZkOYgxpksRiSU3jYegeFGstC50FZmem+co//h0vOj/kBl+7/Aqu33g9Y0eswPm6tlApsxJzLF9C06bWQ1KRFfClKiOR8+LzPqqeVmuExx97nC1PbAHgFRe+lLTRwDml2+vTHwxI0qTGFYWFuQXWHX8cL3nx+fT7PRbmFzBiuOjii+j1+1hrKxUrGW4UkVrjiNT0PHWxIKcOf5VtzItgdhY1k/vc4fKcNElCn0NkBpqi/quB7ZDEos9gMGDf7j0kmvPVL/9P3vOedwNw732b+OhHP06zPRIjHBC1ZXWuuJbUbsuICTyjcsBL00FFmoyNE+rCjTaaTQ7sP8Btd9wBwHOfexanb1jP/MxB1p9yCiuOWMa+XTvpdrpYG2660Whw4OAUBw5MkWUNWu0mxhi2b3+y7BgsW1x1sbimHmJ9Fj9IpH+Wr5sKcql+XRfNVpGYSqWBVzD2JOrTFYjA1PQM+w/uZ/+Bp2k1G7zn0ndy260/4tJLLwHgnns38aY3vpmpmVmMSXC5q5lArQlG+apzMrIvbGNs8rJ6w/Gw6YhtPt7jfR+vkGVNeguzpFmDt731zWRZyr59e/nhD7/Puee9iG9/458ZnxjlqZ272LV7D/1+n/bICAu9Po88/AjPOv00jDFcu/EG/uIvP0uSJLF/dlhvepGk62GmRA75SZ3gqzVIfegHpb+oc1qVsqBLRVIY5DmNzPCWN72OV1z4Uj74gffy55f9Ee+59BKWr1gOwFe/djnvfvel7N8/RWt0FB+EW0s+aqFtWpj6Qm3L+wFuMEAmVx6vJTs6xnuVoETw1upynOuTO2V8/Aj8oMNg0OdHG6/m7HOewxObN/PCF72KqekZNl7zbV58wflMz0xz/fU3ctVV3+SWW3/C9PQMg16HpStXsmL5Cp56aheNRha0mmNTSKFQKHroyNbM6OH2RvVWrWn91AobwacdZkJlkVhHXI1JYtmzawcf++hv8dm//vTQr/T7fW6//U4+/3df5N+/8+80RkZJ08DONlF/whREr6LKVyotBh/k8gGu30MmVh6vReN1XZSj9PjOReTOkec5rdFJGtayf98+PvTB9/K3X/gMAL/3iT/hM3/5N7z4ZS/m2u9/i2azWd7wo48+ysbrb+DmW37C3ffcz44dOyOl25A1MlrNVoAnIrOh2gk11vaiLaBSl0ev+WGG0VeV4clRPbRfrLysrxr+bGKZOriXG354Deee+3y2P7mD737339mydRub7v8pD/70ERbmZxgZbVecBpuEGkAkBxQc04Ly4wvSgYLzOd7lyMTKtYrKIm2g+rOGJgh1YdY0SWi3xnCdBbIs4babN3LiiSewY8dOnnf+hezdsZO//4e/5b+871K63Q5pjIWLP3v3PcN9923i/vsf4PHHt/LAgw+xddt2up0+4xPjpXLCYXfCYjMkh8kM6iIi8ksUs/WX6AZHB2yNYWZ2hrPOPI3rr/8+zWaLV736jfxw4/eQbClJkjA6MU6vMxu4Q2LDgFsbmBLFvzHDylxFA6EPVTLnPbJk1XFRyqYWIdS4/UqgUXvnEe/I3YDm6BKsKgef2c8l73o7X/vqPwDwmb/+PL/38d9n7boTuOPWG1i1ciXOuUqpXGRoMgAWFjps3rqNf73qW/z157/I2MhYyAtMvQnv8KLLQ5IiRZhXSyaFYZxLf5VQdq3+kSQJ+/bt5RtXfZW3vOVN/Mu/fJN3vetiJpYehQlaN3iFzuwBEpuCtZG6HzhLhVSyGFkkZq7l2QXe+4BvTUQook6ZqxQB45udw7k8FObzPiZrB1LVoM/83Bzf+sbXecPrX02ns8DLL3wdd9x+E2+/6N3861Vfo9/vx2ioEkitqxvaqIDS7/d51hnnsHvvAZqNRsVP1eGJWDz4i1dzGf3KcAQqh5mGxddVDTH+3r17+chvfYDPf+EzPPbYY1z4ijcyO7eATRKc86TtEWan96Ouj7FBkcvYFLG2FK+lVmOsGplr8mcxabWNkYnLZJH3r9QQBYnpcykWpZ48H5BlrUBHAW780Y95/etexcqVK9mw/hS++a3vsem+BxkdHeX881+A9y4qJwZ+Tv1LVcnznCzLQAzf/d4PGBsbD7XYIe3/miY01VkBpt7hX2vKllryNNQCVUoWD3kPVCHLMvbu3cfrX/cqvva1f+Cpp3bw1rddws5de2m3WjjnMWnKwA/Iu/OYJAt23oYOHlP2JcuhfmuI/1ShzrY5OnlZZf8PPTVC6/zPQpnW5ThVrE3JEsuBg1P85M67ecfbf43j1x3PsqXL+ME1G7n1J3dz+mmnsn79yeS5K1f7IvYRIoLLHc961ulce90NPLVrJ+1mK0qjDdmaUlQvSS2dhS69Xo9WuxWOGyl2sVY5Rb1Pvs6MXzw+SZKwe+dOLnz5i7nmmm/z4AMP8faLf51HH9/K+PgYeR4CFZtlzM1NR65CwWdKIjvaVNqjRfhJdabBkMR/JPbb5tiSy7Qm6T50fEgpC1mBdQW/P8/7pGkDvDLSHmHzlm08+uijvOH1r+F5zzubgwenuO2WW7nx5lt5yQXnc/TRR9Hv96s+Yhg66UJ9SPJOXX8yl19+RWDRDWkGVvdljaHX7XHRRW9mcskEv3j0MQbdLlnWILG2QK+qLjqGzxqQyL4rBsRYw/T0NG9/+1v5n3/3Wf7mc5/ngx/6CE8/M8342Cj5IA8djWlK3w3od+fDYoot+qagIRbmp9YVWl/xVchb/cM22pOXVdTrapUNJ0G1yCIyhAsUM8ka5IM+o6NjbLr/Ie7fdB8XXvhS3vKWN7F58+P85PYfc93GH3HBBeezZs0a+r0uJkmqZCverLEW5wccd+yxZFnK9757NSPjE6XMV/EkJnI9FxY6LF06xve/901O33AKBw/uZ8uWLezff4BOt0svz+l0OoGyrrFlNmalzjkW5hfo9rtYK3Q6Xc549pmc87zn8Id/8Ed865vfod0ep9lokDtXSeWkKfMzByIr2iImCHsMEXHrLaKL8pZS6zQitQjYxujkZfVzWqpkpqbdLKaUmg9ItY+Z4oC00SjpiWOjozz00M+59trreP65z+PDH/4QP/3ZI9x152187+prOeusMznxxBMZDPqB01NqBRFjZotzjvPPP4+DB6e46cabGZsYK/1B8ZDqPe1Wkwc2PcRJp5zE29/2a7zznRfxhte9mpNPOpGlS0dpNhsce+xajBG63QU63Q79QZ9+v0eaGk466URGR0aZmpqm1W7T6fa45prrWJjvsvSI5WVLrInyxkmWsdCdCw3cJgmhp40TEZOueqOjyGESPa2glnI3jq08tq6HtUj7kKGO46K7O6gXBmxIkoyRkTFcv4sxljTJmJqeot1M+au/+u984P3v5Z3vfDdXXXUF7bEj+Psv/i2XXHJx7EYpCjPDkYjX0Fr6Gx/8CF/+x69wxKrVeOeG1MlFhLnZWdZvOIWbb7yGRjMjTdJD/N701DT7nt7H7Ow8vX6fiYlxUM/lV3yTf/mXbzEzO0eSBLZ1lqYlfaTe2IENfKiZqX0kNgkNeVGOxhgTdgKLJI1rbbhV4KAlAbrAhmxzbMllsEiSXYaEF2oZpRQqMCXxyuV9TNrExEzWe6XVbOG84Vvf/g6/eOwx/vIv/x+StMUtN93Cd66+Du9yLnjxi7DGkud5yW6r8PbQffOGN7yGg1NT3PSjH9Joj2AJEshFKNtsNtmyeQvHH7eW5z73LPr9XhCXqqnhNlstli1bxurVqzh6zVHs2rWbP/6/P8nXL7+KwSAnsbY8qEG9HzYGkW6fZE1mZw+Go1tMDDfjRJRJl9TFE2saTHXPWjD5TKV1ZJsjk5fV+xqq8EyGnUh9G6GxgzWslF6/R6s1UnJeii6XsdEx7rvvQf71G9/mhS+6gBNPPoXNW7Zyw003c8ftd7D+lJNYs2YNIsJg4Kq5N6a8zmte/UqyLOWaH1yDx9BsNctOGBNpk9u3b+fX3/0OsrQRaTISfBRVTP7ww4/wu3/4x3zs43/Eo48+weTERKXuXk8gFoW8JmvQ7Xbpd2ewSRpUWSQmXkL0BTLcFFITEpRS92jRoUXRX9jm6JLL6iBXNWu1RruhxKXIMn0pWq35AO+VZnsUP8jDttSgkzk6OkJ3ocf1GzeWVI683+exx5/giq9fyfTMDKeffhrj42PxhCVXJoIignOOCy54EWed+Sxu+vHNPP3MQcbGx8twstVqs3Xbdk444XjOOON08nwQE78w+HfeeTef/OSn+INP/Cl33/0A42MTtEfaoWkxdq+YGjpaC1qDzJoY5mb2R4zHxmTLhkw9Dn4duxoaMzlM+lcr/yog4yuP1bpC6BDuXivWqFbnQBCL3t4PotlxuMGA0SXLScTi+gPsImVBa4WFuYVAvErCADnnOHhwP8etXcP733cp7373OznmmKPLCS8YCt570jTlyaee4uMf/wTf+e41jI6N0Ww2UOeZm5/nhHXHcdutGxkbG2NmZo7rrtvIV796OT++5Sd0ul3GxyfIsgx1roSj69l+8W+vlPqeJsuYnTmI+j7WBB1qSSLhNhaqwmKrnU+gyiHiF4uzn6IKqT5OQD3R0diCVJaWZGgKi54x9T60IzmH8znqc7wXlixbieaDeGZDTcu/XpDzio+tn9ZaFhYWmJudZdXqFbz+ta/m4ovewnnnPX8IUc3znCSGr1/+8lf45H//K57cvoOx8UlG221279nNl770ebwb8OnP/A1btz6FSTImJ5dgbYL3ea3Xod5GW8XshWnz6pEkYaEzz6A7R5KkwX7bLO6AGHDaALiJLrbTDAGC5TkDdTjWg/MO22hPXHbIbJXcWjMcCLH4YDUZOulTnKfX69JojYJzw0IcQ3mIlJoNXpUszRgbH6fT7XPnnffwr9/8Dldf/QOeePwxev0+oyOjTE5OlJ991lln8u5LLgY8Dz/yMHv27AOEZw4c4Jhjjubb3/4Oy1euodVqlTL6pi5pOSQCO0wAKM6a7A369DuzJGkS+r1MEjp0DBXUrFI75mXYxguLzp+UYQtUnnkzvnJtlZh7rde1h9S7F8sZl9GIV3DFDvA4N8AkDUZHJ0NTXHE242JY9hBMMnTkGGtw6ul0OnS7HawxrFy+nJNPXsfpzzqNs59zFhtOXc9JJ51Ie6TN1NQUX7/iG3znuz/grnvu4bc+/EHu3/RTbr/jTsbHRoLSY8XoGlpGIofKjWMMDs/M1H7SJGL7NgmhZox2TGzSK4PVUsRbF2n1UPZHV8qONfatBxlbsfY6MeaV6pyL0uk1XZ96fE51VlbRQ1CYIR+q/F7zSNXrkzXHGGmP4Qf98tQ8kV99eGsdKTXGxNBW6ff7dHtdur0exghjoyOsWLGCNUeu4sQTT2DDhvWMj42yf/8BpqfnWH3kUfz+H/4JrXYzJHF1jf+hMHPRkhDBG5id2R/1mEIbbJnxliIeJhaUamroMkzQKFkdRUGp1pAo4Xgp653fmAjsXVwn9aXg7JAbrnWSaXUopg8i2mEVWCDHmoRuZ5Y0SWk0WuS9cJjCYde+LD4EoaCOBK1SAWyaMJqNMT4+gaond45du/ey/cmd3Hzrnfg8J2tmLF++jBUrVpA1GmSNUBxf3HJV7uF6qBd5myoSB19jthv0Tgv90upQKy0BtlJ+p86ZGXqmSolSa6oE3jm86l4ZW3nc6wS+p+HUZFOvvS5escOnzxXXLVa/R3xg0nnvUOcY5APGl64klQQ3GARB7noX+2HKJDqcuS+CkKtDDcqjAyOops7Tz3MG/T7ee1qt5mFNXv0MsuLSQVUrYX7uYCSgpdHUhLYsE6tcSk1w6jATgpFD6hZD2udlXBLE39S7Nwhr1rTGesnDoMfWUdPSZi+ucMiw6pv44A+K0zKCOXLhNRd4/ONLVpBg8PmgbKSuFLaqEPfw3MPhFSc1tfKStcHQ2c2Bfe+Hg4YKzq4iskKqXpKEudkp/KAbBj+KLWGKbNdUr5VyVqYGjVQfrq449U8XuYQSoVUFcflg22zmTzXs2NEx6PViTBA15pAy0fDp1DVyKpFBRnnmbqEEEgAqa0IlbObAPpwqkmbl6dslFb5iUJUHnw2TrWr9tVpptoqnonrUQ+8SyxnWu6jOzylMXMgxTJIyPzuFG3RjshWP1jIhV8GY+ml4JWuu2rG+RtsuMDNXPUc0pyWF0vtcVNSoXB/GPvjWq4LAT1CXK+R4Ij24xsurO+RKfrCAlY3URfdM2RliDEwd3ItTh03rqibDeK3IcPb4S9i45WCW+m9FBOcLyrrEw+UKqiXluZdFcQkxeGOZnT6A74fBJw44JmS8xCoXpnYdMRVZoJYqqWjN4ZoqstVK3siHJlGjXgWfXFWE8HZ6//abvHM/NhiLqvNaHddEPLm0xl2nXl6S2olxiKAxAy5vJB7gYA1M799D7nNsoxnOGIgI6xCNkEpNpAzYdLEpqQmL6qE0LaEmq1M7yrA4d1isRW2AGJwL9YlwMrctteCkFCCsG/14VGD9hIziP1/jpUrtsNL6qvXeibXWq/54ev/mm4qDGBXAOvNe77VT7bU4EEbrnRGLAOvFcn2FpHFN4szYmLIHysb0gT30+gvYZiMwLbQ24LVzvorTD9FfQpWpHTLka4ey1mGYIW6sBEiDJMEZZXb6GYhFdSlCzQJaKE+FlZp2nCw6UzjWTArTWxxhZWRR2CWh77CAMb3viLj3Vg1hsTbc600fzEZGDmDS16PqgkVZdHDxEJWjUjlf7OCGUcVK4IgIfHXmZ8AYGu3REGrKoUanEkoy5YKrhACl1LYr5ShFDilA1aMpVcU2M3LvmD24H3DYpBHpJDaaHVMCcMGkmsqkHqIAPkwPrg59o0ZukPoxYE6SJPEu/+2ZfdtviMLJ3tasme13Zu9NWiMnGWPOQBlgxJbYdSFSV8QWi/OE+vnyUkvDy61oynNexBj63S7ee5oj4zjvayfecYhfqB+Stpg/ujihqg9Lea6kGEwjo9udZ2EmHGNVmp2gsFT7vmK0HZI4yjCMUYXrVZdoqZ1a5wMJAzE2Ve+unNm37f+Kpt8tXnbl1I4sO/J+sekZeDdATMrQUT71hIxD7G95MmztaNugoRxlPbyL3HiPz3NMmjE6eQTGhaMKC4ylnifIolPsSnrK4jylfmpfdLYmTcEaZqb24/oLkUhlAs5VZLrWxLJi1H3WCnrHDJ99WcXzVQhcwdjD/iga04ExSep9/uDMnu1nDoNP1YnaQ3FG1py8VUTPFjFHh9C0kCP8FVzBmkZY6YSKwyGKo0kKeC86UWssbpDT7cxjk4wka6JuUNWIhzaWHJJBH452VTGgwWRNnHpmDu7Duz5JmqHGRohBUKmVFQsRqkVs+DqtvTjtZEgNMWhnDtnOWhHXGTEp3t3j8L8+mJ95enH2Kb9qKEeXHX2lWHMx3qFBi90aBW+ocoAhQtcwB0ZLvaFQQRNfNP35qKuZR96pI3c5zfYYI2OTaB5g7uIQh19OLNQhmyyxK99YC0nCwvws3YXpSJiKOm5xpROLKfXBl8Ocnlbf7QUlJ55GFaPN6izNCgISB1ixFu/dVbN7tr2DX8Kw/GUd0xaQfmfm21lzfC9wgRHTjAoerjQMdWW8oWNgq48xZjhXoDgnoNwpURrSGPJBj16nQ9pokDRCsaWuyHs4vL06wTQ44qTRJFfHzMGnGXTnsTEKkxp7AWOj2qGJirdSnYm8qP5BeSzhIlXIuhh4ZHCKqovXt+DnwP/O7J7tnygO2qokwQ7T0/DL0x50YsUxxzvH3wv6cmIKHyZDNMipix2S2o1OSWoC2QUXUrQuXOcQDYJ7GiW8XDy6sNEaYWR0AiMGNxhUhXM9tJ/Se8VmgZe/sDBNZ26maoWNpFkT8xIVi7Ghj6Y4gipSFKpmjf9waMot7kqPELHqqL1xA+I/OL3vyS2LO6n+30xAfTc4gPEla17pRV6v+FcJZl2xMn1N6tjoUDpUKZTXmt0KaXctzhWOqbtXV+pT+HyAiqE1Oka7PQ7O4/JIkiobSQJ+L4ll0O8xPzMNPvSc1bPxsunb2LKYUpw9XzipX0WFP4x9r+T4q8R0M8h1Kv7qmae3bVw8dv/RKv+P/pi6516zZk1rel5fCrxDlWUYeVV5TIiaGpKpQ0ULho7KWoSbxKNSilN1Q6ObxzmHtSljE0tJ04y82y+vY7ME53PmZ6fJ+z1s2RRRHIMVbL1KRR009VM8tGqgQ/lPTsJQvnOdR/cn2CtHmv0bd+zY0VkUUfr/6HL/B6tFgef6RBoAAAAAAElFTkSuQmCC"


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


class _ReplayedBody:
    """The request body, already consumed, served back once.

    Auditing needs the form parameters, and the 26 POST handlers
    each read the body straight off `self.rfile`. Reading it here
    would leave them with nothing. Rather than rewrite all 26 —
    and require every future one to remember a helper — the bytes
    are read once and handed back through this shim, which
    delegates everything it has not buffered to the real socket so
    keep-alive keeps working.
    """

    def __init__(self, buffered, rest):
        self._buf = _io.BytesIO(buffered)
        self._rest = rest

    def read(self, n=-1):
        data = self._buf.read(n)
        if n is not None and n >= 0 and len(data) < n:
            data += self._rest.read(n - len(data))
        return data

    def readline(self, *a):
        line = self._buf.readline(*a)
        return line if line else self._rest.readline(*a)

    def __getattr__(self, name):
        return getattr(self._rest, name)


def create_handler(config, checker, bot, store, password=None, backend=None,
                   hosts=None, restart_discord=None):
    """Create a request handler with access to app components.

    `restart_discord` is supplied by `main` and is the only way this
    module can bring the Discord bot up on new credentials. It stays a
    callback rather than an import because building a DiscordBot needs
    the store, the engine, the host registry, the checker and the
    Telegram bot, and none of that belongs in a request handler. Absent
    (render tests, anything that builds a handler directly) the settings
    save simply writes the values and says a restart is needed.
    """

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

        def _host_views(self, multi=None, store_for=None, own_name=""):
            """One view per managed host, local first.

            Extracted from the status page so `/metrics` and `/api/status`
            report exactly what the page shows, rather than gathering the
            same numbers a second way. Two places asking the same question
            differently is how this project ended up with a Web UI that
            linked somewhere else than Telegram, and a container page that
            disagreed with its own table.
            """
            from container_store import LOCAL_HOST
            import hosts as _hosts_mod
            if store_for is None:
                store_for = lambda h: store  # noqa: E731 — local store only
            if multi is None:
                multi = hosts if (hosts and getattr(hosts, "is_multi", False)) else None
            views = [self._status_view(LOCAL_HOST, store_for(LOCAL_HOST),
                                       self._get_containers(), own_name,
                                       host_backend=backend)]
            # One `ps -q` per host, and the hosts side by side. Measured
            # against four managed hosts: an ssh endpoint cost 2.1 s of
            # which 476 ms was a probe asking what the listing asks again,
            # and the whole set ran one after another, so the page paid the
            # sum. Now it pays the slowest one.
            _todo = [h for h in (multi or ()) if not h.is_local]
            _skip = {}
            for _host in list(_todo):
                _left, _cached = _hosts_mod.unreachable_for(_host.name)
                if _left > 0:
                    _skip[_host.name] = (_cached, int(_left))
                    _todo.remove(_host)

            def _fetch(_h):
                """`(host, containers, error)` — never raises, so one dead
                endpoint cannot take the others down with it."""
                try:
                    _p = _h.backend.ps(quiet=True, timeout=10)
                    if getattr(_p, "returncode", 1) != 0:
                        raise OSError((_p.stderr or "").strip() or "ps failed")
                    _ids = [i for i in (_p.stdout or "").strip().split("\n") if i]
                    return _h, self._containers_on(_h.backend, timeout=10,
                                                   ids=_ids), None
                except Exception as _e:
                    return _h, None, _e

            _results = {}
            if _todo:
                import concurrent.futures as _cf
                with _cf.ThreadPoolExecutor(max_workers=min(8, len(_todo))) as _ex:
                    for _h, _rem, _err in _ex.map(_fetch, _todo):
                        _results[_h.name] = (_rem, _err)

            for _host in (multi or ()):
                if _host.is_local:
                    continue
                if _host.name in _skip:
                    _cached, _left = _skip[_host.name]
                    views.append({"unreachable": _host.name,
                                  "endpoint": _host.endpoint,
                                  "reason": _cached,
                                  "retry_in": _left,
                                  "contexts": []})
                    continue
                _remote, _err = _results.get(_host.name, (None, None))
                if _err is not None:
                    # One dead host is a line in the table, not a broken
                    # page. The CLI's own words go with it: this page used
                    # to say "unreachable" for every cause there is, which
                    # is the one word that fits none of them.
                    _reason = self._why(_err)
                    _hosts_mod.mark_unreachable(_host.name, _reason)
                    views.append({"unreachable": _host.name,
                                  "endpoint": _host.endpoint,
                                  "reason": _reason,
                                  "contexts": self._docker_contexts()})
                    continue
                _hosts_mod.mark_reachable(_host.name)
                views.append(self._status_view(_host.name, _host.store,
                                               _remote or [], own_name,
                                               host_backend=_host.backend))
            return views

        @staticmethod
        def _why(err):
            """The one line of a failed host probe worth putting on screen.

            Both CLIs write a *block* when a remote endpoint fails, and on
            Podman the first line is the least useful thing in it:

                Cannot connect to Podman. Please verify your connection to
                the Linux system using `podman system connection list`, or
                try `podman machine init` and `podman machine start` to
                manage a new Linux VM
                Error: unable to connect to Podman socket: failed to
                connect: ssh: handshake failed: ssh: unable to
                authenticate, attempted methods [none publickey], no
                supported methods remain: ssh://root@nas/…

            Byte-for-byte what podman 4.9.3 printed for a host reachable
            over ssh whose key was refused. Line one sends the reader off
            to `podman machine`, which has nothing to do with it; line two
            says exactly what happened. That held for every failure shape
            measured — refused port, DNS miss, wrong socket path, refused
            key, and Docker's own one-line `Permission denied
            (publickey,password).` — so: last non-empty line, and no
            attempt to classify it. Whatever the CLI chose to say, the
            reader gets, and a future CLI wording cannot fall through a
            pattern we guessed at.

            Clipped to 200 chars, the same budget the Telegram and Discord
            paths already use for this, because the tail of these is the
            endpoint we printed next to it anyway — with an ellipsis when
            it happens, since these end in a URL and a cut one reads as a
            *wrong* one rather than a shortened one.
            """
            text = str(err or "").strip()
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            last = lines[-1] if lines else ""
            return last if len(last) <= 200 else last[:199] + "…"

        #: The endpoint field is named differently by each CLI and neither
        #: accepts the other's. Measured, and the failure modes differ too:
        #:
        #:   podman context ls --format '{{.Name}}|{{.DockerEndpoint}}'
        #:     -> exit 125, "can't evaluate field DockerEndpoint"
        #:   docker context ls --format '{{.Name}}|{{.URI}}'
        #:     -> exit 0, "template parsing error: … can't evaluate field URI"
        #:
        #: Docker returning 0 for a template it could not execute is why
        #: this cannot go by exit code alone: the first version of this
        #: checked only `returncode`, so it produced nothing at all on
        #: Podman and would have handed an error string back as an
        #: endpoint on Docker.
        _CONTEXT_FORMATS = ("{{.Name}}|{{.DockerEndpoint}}",   # Docker
                            "{{.Name}}|{{.URI}}")              # Podman

        def _docker_contexts(self):
            """`[(name, endpoint)]` from the CLI's context list, or [].

            Both CLIs keep one — Docker calls them contexts, Podman calls
            them system connections and accepts `context ls` as an alias
            for it. Whichever we are driving, the other's format string
            fails, so both are tried and the first usable answer wins.

            Best-effort and never raised: this runs while rendering a page
            that is already reporting a failure, and a second failure must
            not become the story.
            """
            for fmt in self._CONTEXT_FORMATS:
                try:
                    r = backend.run(["context", "ls", "--format", fmt],
                                    timeout=5)
                except Exception:
                    return []
                text = (r.stdout or "") + "\n" + (getattr(r, "stderr", "") or "")
                # A template the CLI could not execute says so in the
                # output whether or not it also sets an exit code.
                if getattr(r, "returncode", 1) != 0 or "can't evaluate field" in text:
                    continue
                out = []
                for line in (r.stdout or "").splitlines():
                    name, sep, endpoint = line.partition("|")
                    if name.strip() and sep and endpoint.strip():
                        out.append((name.strip(), endpoint.strip()))
                if out:
                    return out
            return []

        def _updating_now(self, host_name):
            """`{container name: target version}` being updated right now
            on `host_name`.

            Read straight off the update engine — the same object whose
            lock every update flow takes — rather than kept a second time
            here, so the badge cannot disagree with what the log says. An
            install without a bot (the render tests, a bare probe) has no
            engine and honestly reports nothing in flight.
            """
            from container_store import split_host_key
            try:
                live = bot.engine.updating
            except Exception:
                return {}
            out = {}
            for key, version in (live or {}).items():
                khost, kname = split_host_key(key)
                if khost == host_name:
                    out[kname] = version or ""
            return out

        def _own_container_name_safe(self):
            """Docksentry's own container name, or "" if it cannot be found."""
            try:
                return checker._own_container_name() or ""
            except Exception:
                return ""

        def _machine_state(self):
            """The numbers both machine endpoints report, gathered once.

            Deliberately reads the same files the Web UI reads rather than
            re-running a check: an endpoint that triggered work would let
            anyone with a scrape interval drive the update loop.
            """
            from container_store import LOCAL_HOST
            state = {"containers": 0, "pending": 0, "hosts": {}, "per_host": {}}
            try:
                views = self._host_views()
            except Exception:
                views = []
            for v in views or []:
                host = v.get("host") or LOCAL_HOST
                if v.get("unreachable"):
                    state["hosts"][host] = 0
                    continue
                state["hosts"][host] = 1
                cs = v.get("containers") or []
                pend = v.get("pending") or []
                state["containers"] += len(cs)
                state["pending"] += len(pend)
                state["per_host"][host] = {
                    "containers": len(cs),
                    "pending": len(pend),
                    "names": sorted(u.get("name", "") for u in pend),
                }
            return state

        def _serve_metrics(self):
            """Prometheus text exposition.

            The motive that generalises best, from the projects surveyed:
            people who will not allow unattended updates run the tool in
            report-only mode — and for them the metric IS the product. So
            this reports what is pending, not just that the process is
            alive.
            """
            st = self._machine_state()
            from version import VERSION
            lines = [
                "# HELP docksentry_up Whether Docksentry is responding.",
                "# TYPE docksentry_up gauge",
                "docksentry_up 1",
                "# HELP docksentry_build_info Version, as a label.",
                "# TYPE docksentry_build_info gauge",
                f'docksentry_build_info{{version="{VERSION}"}} 1',
                # Not `_total`: Prometheus reserves that suffix for
                # counters, and this is a gauge. promtool rejects the
                # mismatch, which is how it was caught before shipping.
                "# HELP docksentry_containers Containers being watched.",
                "# TYPE docksentry_containers gauge",
                f"docksentry_containers {st['containers']}",
                "# HELP docksentry_updates_pending Updates found and not yet applied.",
                "# TYPE docksentry_updates_pending gauge",
                f"docksentry_updates_pending {st['pending']}",
                "# HELP docksentry_host_up Whether a managed host answered.",
                "# TYPE docksentry_host_up gauge",
            ]
            for host, up in sorted(st["hosts"].items()):
                lines.append(f'docksentry_host_up{{host="{_metric_label(host)}"}} {up}')
            lines += [
                "# HELP docksentry_host_containers Containers per host.",
                "# TYPE docksentry_host_containers gauge",
            ]
            for host, d in sorted(st["per_host"].items()):
                lines.append(f'docksentry_host_containers{{host="{_metric_label(host)}"}} '
                             f'{d["containers"]}')
            lines += [
                "# HELP docksentry_host_updates_pending Pending updates per host.",
                "# TYPE docksentry_host_updates_pending gauge",
            ]
            for host, d in sorted(st["per_host"].items()):
                lines.append(f'docksentry_host_updates_pending{{host="{_metric_label(host)}"}} '
                             f'{d["pending"]}')
            # Per container, so an alert can name the thing that needs
            # attention rather than only a count.
            lines += [
                "# HELP docksentry_container_update_available 1 when this container has an update.",
                "# TYPE docksentry_container_update_available gauge",
            ]
            for host, d in sorted(st["per_host"].items()):
                for name in d["names"]:
                    if not name:
                        continue
                    lines.append(
                        f'docksentry_container_update_available'
                        f'{{host="{_metric_label(host)}",container="{_metric_label(name)}"}} 1')
            body = ("\n".join(lines) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_status_json(self):
            """The same numbers as JSON, for anything that is not Prometheus."""
            from version import VERSION
            st = self._machine_state()
            payload = json.dumps({
                "version": VERSION,
                "containers": st["containers"],
                "updates_pending": st["pending"],
                "hosts": [
                    {"name": h,
                     "reachable": bool(st["hosts"].get(h)),
                     "containers": st["per_host"].get(h, {}).get("containers", 0),
                     "updates_pending": st["per_host"].get(h, {}).get("pending", 0),
                     "pending": st["per_host"].get(h, {}).get("names", [])}
                    for h in sorted(st["hosts"])
                ],
            }, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _api_token_supplied(self):
            """Whether the request presented a token at all, right or wrong.

            The difference matters: no token is a browser and may fall
            through to the password; a wrong token is a failed
            authentication and must be told so.
            """
            if self.headers.get("Authorization", "").startswith("Bearer "):
                return True
            return bool((parse_qs(urlparse(self.path).query).get("token") or [""])[0].strip())

        def _api_token_name(self):
            """The name of the API token this request carries, or "".

            Read-only endpoints (`/metrics`, `/api/status`) accept a token
            instead of the Web UI password, because a Prometheus scraper
            cannot log in and a shared browser password is the wrong thing
            to hand a scraper anyway. `API_TOKENS=prom:xxx,grafana:yyy`
            names them so one can be revoked without disturbing the other.

            Bearer header preferred; `?token=` accepted because several
            scrapers cannot set headers, and refusing them would push
            people towards leaving the endpoint open instead. A token in a
            query string does end up in access logs, which is a real cost —
            documented rather than hidden.

            Compared with `hmac.compare_digest`: these are secrets, and a
            plain `==` leaks their length and prefix through timing.
            """
            import hmac as _hmac
            configured = getattr(config, "api_tokens", []) or []
            if not configured:
                return ""
            supplied = ""
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                supplied = auth[7:].strip()
            if not supplied:
                q = parse_qs(urlparse(self.path).query)
                supplied = (q.get("token") or [""])[0].strip()
            if not supplied:
                return ""
            for entry in configured:
                name, _, token = entry.partition(":")
                if not token:
                    continue
                if _hmac.compare_digest(token.strip(), supplied):
                    label = name.strip() or "token"
                    # One seam for "was this token used?", here rather than
                    # at the two endpoints: /metrics and /api/status both
                    # come through this, and the third one to be added
                    # would otherwise be missing from the page without
                    # anybody noticing.
                    seen = getattr(getattr(self, "server", None),
                                   "token_seen", None)
                    if seen is not None:
                        seen[label] = time.time()
                    return label
            return ""

        def _sessions(self):
            """The server's session store, or None on a bare test handler."""
            return getattr(getattr(self, "server", None), "sessions", None)

        def _cookie(self, name):
            """One cookie value from the request, or ""."""
            raw = self.headers.get("Cookie") or ""
            for part in raw.split(";"):
                key, _, value = part.strip().partition("=")
                if key == name:
                    return value
            return ""

        def _session_user(self):
            """The signed-in username, or None. Also renews the session."""
            store = self._sessions()
            if store is None:
                return None
            return store.validate(self._cookie(SESSION_COOKIE))

        def _check_auth(self):
            """Is this request allowed? A session, or Basic Auth, or no
            password set at all.

            Reads `config.web_password` fresh on every request rather than
            a value cached at startup, so a password changed in Settings ›
            General takes effect immediately — no restart. (The `password`
            argument to create_handler is just the startup value of the
            same field; config is the source of truth.)

            Basic Auth stays, and not for old times' sake: `curl -u` and
            every existing script and scraper use it, and a login form
            cannot serve them. What the form adds is a page a password
            manager can actually fill in, which the browser's own Basic
            Auth dialog never was (#60, @NotRetarded).

            The stored password may be a scrypt hash or plaintext —
            `webauth.verify` handles both, because `WEB_PASSWORD` in the
            environment can only ever be the latter.
            """
            # getattr, not config.web_password directly: the real Config
            # always carries it (a constructor arg), so this is a no-op in
            # production — but it keeps auth from 500-ing a request on a
            # config that somehow lacks the attribute, degrading to the
            # documented "no password set → open" instead of crashing.
            current = getattr(config, "web_password", "") or ""
            if not current:
                return True
            if self._session_user() is not None:
                return True
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                user, pw = decoded.split(":", 1)
            except Exception:
                return False
            want_user = getattr(config, "web_username", "") or ""
            return (webauth.username_matches(user, want_user)
                    and webauth.verify(pw, current))

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

        def _send_auth_required(self, path=""):
            """Send a browser to the login page, a script to Basic Auth.

            The distinction matters both ways round. A person gets a page
            a password manager can fill in, which is the whole point of
            #60. A script gets the 401 with `WWW-Authenticate` it has
            always got, so `curl -u` and every existing scraper keep
            working — redirecting those to an HTML form would break them
            silently, which is worse than not having the form at all.

            There is a third caller, and missing it is what put the
            browser's own password box back on @NotRetarded's screen after
            all this work to replace it (#60). Our own pages fetch in the
            background — the Settings page asks `/api/cron_preview` on
            load. A `fetch()` sends `Accept: */*`, so it took the script
            branch, and `WWW-Authenticate` is precisely what makes a
            browser pop that dialog. He left a tab open overnight, the
            session expired, and the restored page's first background call
            asked him for a password in the browser's voice rather than
            ours.

            Browsers label those requests: `Sec-Fetch-Mode` is set on
            every fetch/XHR and cannot be spoofed by page script, and no
            command-line client sends it. So a background call from a page
            gets a plain 401 with no `WWW-Authenticate` — no dialog — and
            `app.js` turns that into a trip to the login page instead.
            """
            wants_html = "text/html" in (self.headers.get("Accept") or "")
            api = path.startswith(("/api/", "/metrics"))
            if wants_html and not api:
                nxt = quote(path or "/", safe="/?=&")
                self.send_response(302)
                self.send_header("Location", f"/login?next={nxt}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # A fetch/XHR from one of our own pages. Answer 401 so the
            # caller knows, but WITHOUT the header that summons the
            # browser's password box.
            from_page = bool(self.headers.get("Sec-Fetch-Mode"))
            self.send_response(401)
            if not from_page:
                self.send_header("WWW-Authenticate",
                                 'Basic realm="Docksentry"')
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

        def _containers_on(self, be, timeout=None, ids=None):
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
            # `ids` lets a caller that already ran `ps` hand its result
            # over. The host list used to probe with one `ps` and then land
            # here, which ran a second one — the same question twice, and
            # over ssh that was 476 ms of the 2.1 s a remote host cost.
            if ids is None:
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
                    # Point a bare repo URL at its releases page, exactly
                    # as the bot's resolver does. This was MISSING here:
                    # the Web UI keeps its own copy of the priority chain
                    # (to avoid one `docker inspect` per table row) and
                    # that copy skipped the rewrite — so the same
                    # container linked to /releases/latest in Telegram
                    # and to the repo front page in the Web UI. Reported
                    # by @LeeNX in #52, who noticed it on Docksentry's own
                    # row. Only auto-detected links are rewritten; a
                    # `docksentry.link` label or a manual /setlink returns
                    # above this point and is never touched.
                    return LinkResolver.prefer_release_url(v), kind
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

        def _render_page(self, content, active="status", wide=None):
            # Only for a session. Basic Auth has no logout — the browser
            # keeps re-sending the header — so a button that claimed to
            # sign you out and then did nothing would be a lie. It is
            # absent in that case, which is honest and costs nothing.
            logout_html = ""
            if self._session_user() is not None:
                _t = _web_translator(config.language)
                # An inline SVG, not the U+23FB power symbol it used to be:
                # that character is missing from a lot of system fonts and
                # rendered as a tofu box for @NotRetarded (#60). The theme
                # toggle right next to it was already an SVG for the same
                # reason — a glyph you cannot guarantee is a glyph you
                # should not ship in a control.
                logout_html = (
                    f'<a href="/logout" class="btn-icon" '
                    f'title="{_e(_t("web_logout"))}" aria-label="{_e(_t("web_logout"))}">'
                    f'<svg viewBox="0 0 24 24" width="18" height="18" fill="none" '
                    f'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
                    f'stroke-linejoin="round">'
                    f'<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>'
                    f'<polyline points="16 17 21 12 16 7"/>'
                    f'<line x1="21" y1="12" x2="9" y2="12"/>'
                    f'</svg></a>')
            from version import VERSION
            from maintenance import get_state as _maint_state, format_remaining as _maint_remaining
            t = _web_translator(config.language)

            # Self-update, in the icon bar (#2, @LeeNX: "I keep having to
            # go looking for it in the settings and always battle to find
            # it, as it's under Cleanup, which seems so odd"). Same POST to
            # /api/selfupdate and the same confirm dialog as the button in
            # Settings › Cleanup — one trigger, two places to reach it, so
            # there is nothing to keep in step.
            #
            # Shown only when a self-update is possible at all, decided the
            # way the rest of the UI decides it: whether we can identify
            # our own container. On QNAP and some Podman setups that comes
            # back empty, `is_self` never fires on any row, and the swap
            # this button asks for cannot be performed either — so the
            # button would be a promise we cannot keep.
            #
            # Deliberately NOT gated on "is there a newer image?": the one
            # in Settings is not either, @LeeNX asked for "the same force
            # update now button like the others", and answering that
            # question means a synchronous registry round-trip on every
            # render of every page.
            #
            # An inline SVG for the same reason the logout and theme icons
            # are (#60): the emoji ⬆️ from _ICONS is a font gamble, and the
            # three controls sit next to each other.
            selfupdate_html = ""
            if self._own_container_name_safe():
                selfupdate_html = (
                    f'<form method="POST" action="/api/selfupdate" class="header-form" '
                    f'data-confirm="{_e(t("web_confirm_selfupdate"))}" '
                    f'data-confirm-title="{_e(t("web_maintenance_selfupdate"))}" '
                    f'data-confirm-label="{_e(t("web_confirm_selfupdate_btn"))}" '
                    f'data-confirm-danger="1">'
                    f'<button type="submit" class="btn-icon" '
                    f'title="{_e(t("web_selfupdate_toolbar_tt"))}" '
                    f'aria-label="{_e(t("web_maintenance_selfupdate"))}">'
                    f'<svg viewBox="0 0 24 24" width="18" height="18" fill="none" '
                    f'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
                    f'stroke-linejoin="round">'
                    f'<circle cx="12" cy="12" r="9"/>'
                    f'<polyline points="8 12 12 8 16 12"/>'
                    f'<line x1="12" y1="8" x2="12" y2="16"/>'
                    f'</svg></button></form>')

            nav_items = [
                ("status", f'📊 {t("web_nav_status")}', "/"),
                ("groups", f'📦 {t("web_nav_groups")}', "/groups"),
                ("history", f'📋 {t("web_nav_history")}', "/history"),
                ("logs", f'📜 {t("web_nav_logs")}', "/logs"),
                ("connections", f'🔔 {t("web_nav_connections")}', "/connections"),
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
            # The status page has been 1400px wide since #46, because the
            # old seven-column table scrolled sideways on a monitor with
            # room to spare. V2's list has no such problem, and inheriting
            # the exception meant the header, the nav and the content all
            # jumped between two widths as you moved between pages — which
            # the owner spotted straight away. So it is a parameter now,
            # defaulting to the old behaviour, and V2 opts out.
            if wide is None:
                wide = (active == "status")
            body_class = "mode-simple" if ui_mode == "simple" else "mode-advanced"
            # Which status view this instance draws. The marker goes on
            # <body> so the stylesheet, the tests and a screenshot can all
            # tell which one they are looking at.
            v2 = getattr(config, "status_view", "table") == "list"
            v2_class = " ui-v2" if v2 else ""
            ui_gen = "v2" if v2 else "v1"
            v2_css = (f'\n<link rel="stylesheet" href="/static/v2.css?v={VERSION}">'
                      if v2 else "")
            # `ui-v2` is the class the list view's stylesheet hangs off;
            # `data-ui` is what a test or a screenshot reads.
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
<link rel="apple-touch-icon" href="/static/icon.png?v={VERSION}">
<!-- Tab icon and app icon are the same file as the brand tile in the
     header below — deliberately. Until now the tab showed a hand-drawn
     SVG (shield + eye) that shared nothing with the logo on the page, so
     a pinned tab and the page it opens looked like two products. One
     256px PNG now feeds the tab, the iOS home screen and the manifest;
     the header carries a downscaled copy of the very same artwork.
     A file reference, not an inline data: URI, because the icon is
     fetched once and cached for a day, whereas an inline copy would ride
     along on every HTML response for no benefit. -->
<link rel="icon" type="image/png" href="/static/icon.png?v={VERSION}">
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
<link rel="stylesheet" href="/static/app.css?v={VERSION}">{v2_css}
</head>
<body class="{body_class}{v2_class}" data-ui="{ui_gen}">
<div class="header">
<div class="header-row{" wide" if wide else ""}">
<!-- Brand lockup: tile + wordmark. The wordmark is real text, not part
     of the image, for two reasons. It stays sharp at any pixel density
     instead of being resampled like the bitmap next to it, and it picks
     up var(--text)/var(--accent), so it follows the theme toggle — a
     baked-in wordmark would have to be light on dark and dark on light
     and can only be one of them. alt="" on the tile because the text
     immediately after it already says the name; a screen reader would
     otherwise announce "Docksentry" twice. -->
<div class="header-brand">
<img src="data:image/png;base64,{_LOGO_B64}" alt="">
<h1>DOCK<span>SENTRY</span></h1>
</div>
<div class="header-host-slot"><!-- v2.0: host selector slot --></div>
<!-- .header-form (app.css): inline-flex, not inline — an inline form
     participates in baseline layout and sat a few px lower than its
     flex-child sibling (the theme button). And margin-top:0, which kills
     the remaining 4px offset coming from the global 8px form margin-top
     — both halves of the misalignment @LeeNX screenshotted in #46. -->
{selfupdate_html}<form method="POST" action="/api/ui_mode" class="header-form">
<input type="hidden" name="mode" value="{ui_mode_other}">
<button type="submit" class="btn-icon" title="{ui_mode_toggle_title}">{ui_mode_icon}</button>
</form>
{logout_html}<button type="button" id="ds-theme-toggle" class="btn-icon" title="Toggle theme">
<svg id="ds-theme-icon-dark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:none"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>
<svg id="ds-theme-icon-light" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
</button>
</div>
<div class="nav-wrap{" wide" if wide else ""}"><nav>{nav_html}</nav></div>
</div>
{maint_banner}<div class="content{" wide" if wide else ""}">
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

        # ── login ────────────────────────────────────────────────
        def _login_html(self, t, error="", nxt="/"):
            """The login page: a real form on a real page.

            Deliberately standalone rather than `_render_page`: the frame
            carries navigation to pages you cannot open yet, and a menu
            that 401s on every click is a worse first impression than no
            menu. `autocomplete` attributes are the entire point of the
            exercise — they are what tells a password manager that this is
            a login form and which field is which, and the browser's own
            Basic Auth dialog could never say that.
            """
            note = (f'<p class="login-error">{_e(error)}</p>' if error else "")
            # The username field starts empty on purpose, even when
            # WEB_USERNAME is set. Pre-filling it would hand the configured
            # name to every unauthenticated visitor, which undoes the point
            # of the deliberately vague "wrong username or password"
            # message: an attacker who can already read the name only has
            # the password left to guess. A password manager fills the name
            # from the URL regardless, so nothing is lost by leaving it blank.
            return f"""<!DOCTYPE html>
<html lang="{_e(config.language)}"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(t("web_login_title"))} · Docksentry</title>
<script>
// Same pre-paint theme resolution as every other page. Without it the
// login page carried a bare data-theme="auto", which the CSS (attribute-
// driven, no prefers-color-scheme media query) left unresolved — so it
// rendered dark for everyone, ignoring a stored or OS light preference.
(function() {{
    try {{
        var saved = localStorage.getItem('ds-theme');
        var theme = saved || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
        if (theme === 'light') document.documentElement.setAttribute('data-theme', 'light');
    }} catch(e) {{}}
}})();
</script>
<link rel="stylesheet" href="/static/app.css">
<link rel="icon" href="/static/icon.png">
</head><body class="login-body">
<main class="login-card">
  <div class="login-brand header-brand">
    <img src="data:image/png;base64,{_LOGO_B64}" alt="">
    <h1>DOCK<span>SENTRY</span></h1>
  </div>
  <p class="login-intro">{_e(t("web_login_intro"))}</p>
  {note}
  <form method="POST" action="/login">
    <input type="hidden" name="next" value="{_e(nxt)}">
    <label for="lg-user">{_e(t("web_login_user"))}</label>
    <input type="text" id="lg-user" name="username" autocomplete="username"
           autofocus>
    <label for="lg-pw">{_e(t("web_login_password"))}</label>
    <input type="password" id="lg-pw" name="password"
           autocomplete="current-password" required>
    <button type="submit" class="btn">{_e(t("web_login_submit"))}</button>
  </form>
</main>
</body></html>"""

        def _page_login(self):
            t = _web_translator(config.language)
            params = parse_qs(urlparse(self.path).query)
            nxt = self._safe_next((params.get("next") or ["/"])[0])
            # Already signed in, or no password set at all: there is
            # nothing to log in to.
            if self._check_auth():
                return self._redirect(nxt)
            body = self._login_html(t, "", nxt).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # A login page must never be cached: the next person on this
            # machine would get it from disk with the form state intact.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        @staticmethod
        def _safe_next(value):
            """Where to go after login, if it is somewhere on this site.

            Only a path, never a URL: `?next=https://elsewhere/` would
            make our own login page into an open redirect, which is a
            phishing primitive and costs nothing to refuse. `//host` is
            a protocol-relative URL and is refused for the same reason —
            and so is a leading slash-backslash, which the browser
            normalises to `//host` and which an earlier version let through.

            Control characters are refused outright, not because a path
            contains them but because this value goes into a `Location:`
            header: a raw CR or LF would end the header and let the rest
            of `next` inject headers of its own (a `Set-Cookie`, a cache
            directive). `.strip()` only trims the ends, so an embedded
            `\r\n` survived it — this checks the whole string.
            """
            value = (value or "/").strip()
            if any(c in value for c in "\\\r\n\t") or "\x00" in value:
                return "/"
            if not value.startswith("/") or value.startswith("//"):
                return "/"
            return value

        def _redirect(self, where):
            self.send_response(302)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _do_login(self, params):
            """POST /login. Verifies, then hands out a session cookie."""
            t = _web_translator(config.language)
            nxt = self._safe_next((params.get("next") or ["/"])[0])
            user = (params.get("username") or [""])[0]
            pw = (params.get("password") or [""])[0]
            current = getattr(config, "web_password", "") or ""
            store = self._sessions()
            ok = (webauth.username_matches(user, getattr(config, "web_username", ""))
                  and webauth.verify(pw, current))
            audit = getattr(getattr(self, "server", None), "audit", None)
            if not ok:
                # Deliberately one message for a wrong name and a wrong
                # password: saying which was wrong tells a stranger that
                # the other one was right.
                if audit is not None:
                    try:
                        audit.record("web", user or "?", "/login",
                                     "failed", {})
                    except Exception:
                        pass
                # A small, fixed delay. Not a rate limiter — it is one
                # line and it turns "thousands of guesses a second" into
                # "a few", which for a LAN dashboard is the difference
                # that matters.
                time.sleep(0.5)
                body = self._login_html(t, t("web_login_failed"), nxt).encode()
                self.send_response(401)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(body)
            token = store.create(user) if store is not None else ""
            if audit is not None:
                try:
                    audit.record("web", user or "-", "/login", "ok", {})
                except Exception:
                    pass
            self.send_response(302)
            self.send_header("Location", nxt)
            # HttpOnly so a script cannot read it, SameSite=Lax so it is
            # not sent on a cross-site POST, and no Secure flag: this is
            # served over plain HTTP by design (TLS belongs in the reverse
            # proxy) and Secure would stop the cookie working entirely.
            max_age = int(getattr(config, "web_session_hours", 8)) * 3600
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
                f"Max-Age={max_age}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _do_logout(self):
            store = self._sessions()
            if store is not None:
                store.destroy(self._cookie(SESSION_COOKIE))
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie",
                             f"{SESSION_COOKIE}=; Path=/; HttpOnly; "
                             f"SameSite=Lax; Max-Age=0")
            self.send_header("Content-Length", "0")
            self.end_headers()

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
            path = self._get_path()
            # Read-only machine endpoints, checked BEFORE the browser
            # password. A scraper cannot log in, and handing it the shared
            # Web UI password would give a monitoring job the ability to
            # stop containers. These two do nothing but read, so a token
            # that reaches them can do nothing but read — which is the
            # read-only role, without a user model behind it.
            if path.split("?")[0] in ("/metrics", "/api/status"):
                who = self._api_token_name()
                if who:
                    return (self._serve_metrics() if path.startswith("/metrics")
                            else self._serve_status_json())
                if self._api_token_supplied():
                    # A token was presented and it was wrong. Falling
                    # through to the password check would answer 200 on an
                    # instance with no WEB_PASSWORD, so a revoked token
                    # would appear to keep working — the operator would
                    # believe they had cut access and they would not have.
                    self.send_error(401, "Invalid API token")
                    return
                # No token at all: fall through to the password check, so a
                # logged-in human can open these in a browser and see
                # exactly what their scraper will get.
            # The login page and the stylesheet it needs come before the
            # auth gate, or the page that lets you in would need you to be
            # in already.
            bare = path.split("?")[0]
            if bare == "/login":
                return self._page_login()
            if bare == "/logout":
                return self._do_logout()
            # The static assets come before the gate too, and they have to:
            # the login page links its stylesheet and its icon, and a 401
            # with `WWW-Authenticate` on those is exactly what pops the
            # browser's password dialog — the one thing the login page
            # exists to replace. It rendered unstyled as well. They carry
            # no data of yours: the same CSS, JS and icon for everyone.
            if bare in ("/static/app.css", "/static/app.js",
                        "/static/v2.css", "/static/v2.js",
                        "/static/manifest.webmanifest", "/static/icon.png"):
                return self._serve_static(bare.rsplit("/", 1)[1])
            if not self._check_auth():
                return self._send_auth_required(path)
            if path.split("?")[0] == "/metrics":
                return self._serve_metrics()
            if path.split("?")[0] == "/api/status":
                return self._serve_status_json()
            # (The static assets used to be served here, after the gate.
            # They moved above it so the login page can style itself, which
            # made this second copy dead — the block above returns first.)
            # Plenty of things ask for /favicon.ico by convention —
            # bookmark managers, feed readers, link previewers, older
            # browsers — and every one of them got a 404 (#2,
            # @NotRetarded asked for a favicon and it looked done from a
            # tab, which is why the gap survived). Same image as the
            # <link rel="icon"> above, served under the legacy name. The
            # extension lies about the format, but every client that asks
            # for /favicon.ico sniffs the bytes rather than trusting the
            # name — .ico has never been the only thing served there.
            if path == "/favicon.ico":
                return self._serve_static("icon.png")
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
            elif path == "/connections":
                self._page_connections()
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
                import backup as _backup
                payload = _backup.payload(config, store, VERSION)
                fname = _backup.filename(config)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            elif path == "/api/v2/status":
                # Everything the rebuilt status page draws, in one
                # document. Built from the same `_status_view` dicts the
                # old table renders from, deliberately: two readers of
                # the store is two answers to "is this pinned?", and the
                # one that is wrong is always the one nobody is looking
                # at. Behind the same auth as every other page.
                import web_v2
                from container_store import LOCAL_HOST, host_key
                t = _web_translator(config.language)
                try:
                    own_name = checker._own_container_name()
                except Exception:
                    own_name = ""
                multi = _multi_hosts()
                views = self._host_views(multi, _store_for, own_name)
                extra = {}
                try:
                    disk_pct, disk_free, _tot = checker.get_disk_usage()
                    extra["disk"] = disk_pct
                    extra["disk_free"] = round(disk_free / (1024 ** 3))
                except Exception:
                    pass
                # A read-only API token gets a read-only page: the
                # actions it would be refused are not offered. The
                # endpoints still enforce it themselves — this is the
                # interface agreeing with them, not replacing them.
                body = json.dumps(web_v2.payload(
                    views, host_key, extra,
                    can=web_v2.capabilities(
                        read_only=bool(self._api_token_name())),
                    # The V2 client has its own hardcoded label table, so
                    # the one new string is worded here — where `t` reads
                    # app/lang/ like every other translation.
                    updating_label=lambda v: _updating_label(t, v))
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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
                            # The page renderer binds its own `t`; this is an
                            # API branch and has none, so bind one here.
                            _t = _web_translator(config.language)
                            now = _dt.now()
                            formatted = []
                            # Label the first entry, so the row reads as a
                            # sentence rather than three bare times. Someone
                            # looking at "18:23 · today 21:23 · tomorrow"
                            # cannot tell which of them already happened
                            # (#2, @NotRetarded).
                            first = True
                            for t_dt in ticks:
                                delta = t_dt - now
                                if delta.total_seconds() < 2 * 3600:
                                    # Less than 2 hours away: HH:MM only
                                    formatted.append(t_dt.strftime("%H:%M"))
                                elif t_dt.date() == now.date():
                                    formatted.append(_t("web_cron_today",
                                                       time=t_dt.strftime("%H:%M")))
                                elif (t_dt.date() - now.date()).days == 1:
                                    formatted.append(_t("web_cron_tomorrow",
                                                       time=t_dt.strftime("%H:%M")))
                                elif (t_dt.date() - now.date()).days < 7:
                                    # NOT strftime("%a"): that reads the process
                                    # locale, which in the container is C, so a
                                    # German UI was showing "Tue 18:00" next to
                                    # "today"/"tomorrow" that were hardcoded
                                    # English in the first place.
                                    days = _t("web_weekdays_short").split(",")
                                    wd = days[t_dt.weekday()].strip() \
                                        if len(days) == 7 else t_dt.strftime("%a")
                                    formatted.append(f"{wd} {t_dt.strftime('%H:%M')}")
                                else:
                                    formatted.append(t_dt.strftime("%Y-%m-%d %H:%M"))
                            if formatted:
                                formatted[0] = _t("web_cron_next",
                                                  time=formatted[0])
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
                # No longer linked from the wizard — the password step is
                # the first thing now and its own "no password" tick is the
                # deliberate escape. The endpoint stays only so a bookmark
                # to it doesn't 404, and it refuses to open a passwordless
                # dashboard: without a password set it sends you back to the
                # wizard rather than past it. The password decision cannot
                # be jumped by URL.
                if not (getattr(config, "web_password", "") or ""):
                    self._send_redirect("/setup")
                    return
                config.web_setup_done = True
                config.save_persistent()
                self._send_redirect("/")
            elif path.startswith("/container/"):
                self._page_container(path[len("/container/"):])
            else:
                self._send_html("<h1>404</h1>", 404)

        def _audit_actor(self):
            """Who is making this request, as far as we can tell."""
            token = self._api_token_name()
            if token:
                return f"token:{token}", "api"
            hdr = self.headers.get("Authorization", "")
            if hdr.startswith("Basic "):
                try:
                    import base64
                    user = base64.b64decode(hdr[6:]).decode(
                        "utf-8", "replace").split(":", 1)[0]
                    if user:
                        return user, "web"
                except Exception:
                    pass
            # No password configured: the Web UI is open on the LAN and
            # there is genuinely no identity to record. Saying "web"
            # rather than inventing one keeps the log honest.
            return "web", "web"

        def _audit_post(self, path):
            """Record one accepted state-changing request."""
            audit = getattr(getattr(self, "server", None), "audit", None)
            if audit is None:
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                length = 0
            params = {}
            if 0 < length <= 1_000_000:
                body = self.rfile.read(length)
                self.rfile = _ReplayedBody(body, self.rfile)
                try:
                    params = parse_qs(body.decode("utf-8", "replace"))
                except Exception:
                    params = {}
            actor, source = self._audit_actor()
            target = ""
            for key in ("name", "container", "group", "id"):
                if params.get(key):
                    target = params[key][0]
                    break
            audit.record(source, actor, path, target, params)

        def do_POST(self):
            # Signing in is the one POST that cannot require being signed
            # in. It still goes through the CSRF check below, which is
            # what stops another site posting a login form at us.
            if self._get_path() == "/login":
                if not self._check_csrf():
                    return self._send_forbidden("CSRF check failed")
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                return self._do_login(parse_qs(body, keep_blank_values=True))
            if not self._check_auth():
                return self._send_auth_required(self._get_path())
            # CSRF mitigation: every POST must originate from the same host.
            # Forged cross-origin POSTs (from a malicious site abusing the
            # admin's cached Basic Auth credentials) are rejected here.
            if not self._check_csrf():
                return self._send_forbidden("CSRF check failed")
            path = self._get_path()
            # One seam for the whole front end (v2.1 audit trail). Placed
            # here rather than in each handler because there are 26
            # state-changing endpoints today: instrumenting them one by one
            # means the 27th is added without a line, and a gap in an audit
            # log is worse than no log at all — a missing entry reads as
            # evidence that nothing happened. After auth and CSRF, so only
            # accepted requests are recorded; a rejected one never reached
            # any state to change.
            self._audit_post(path)
            # Same seam, second job: a state-changing request means the
            # last local copy is now out of date. Doing it here rather
            # than at each of the twenty-odd endpoints is the same
            # argument the audit log makes one comment up — the twenty-
            # first is added without it otherwise. Debounced inside, and
            # it never raises: a backup that cannot be written must not
            # cost the user the change they just made.
            try:
                import backup as _backup
                from version import VERSION as _V
                _backup.write_local_if_stale(config, store, _V)
            except Exception:
                pass
            if path == "/settings":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                # keep_blank_values, or no text field on this page can
                # ever be emptied. parse_qs drops a `name=` carrying an
                # empty value by default, and every branch below is
                # guarded by a membership test — so the one submission
                # that means "clear this" was indistinguishable from the
                # field never having been sent. The page said "saved",
                # the field came back filled, and nothing said why.
                # Measured on a running instance against the Discord
                # webhook, which is on the Connections page now; the
                # quiet hours and the exclude list had it too.
                #
                # Every branch below was walked before this changed.
                # Nothing breaks on an empty string: the numeric fields
                # already swallow the ValueError from int(""), `language`
                # and `cron_schedule` ignore a value they cannot use, and
                # `web_password` treats empty as "unchanged" on purpose.
                params = parse_qs(body, keep_blank_values=True)

                # --- Validate before mutating any state ---
                errors = []
                if "cron_schedule" in params and params["cron_schedule"][0].strip():
                    ok, err = _validate_cron(params["cron_schedule"][0].strip())
                    if not ok:
                        errors.append(f"Cron schedule: {err}")
                if errors:
                    # `quote` comes from the module-level import. It used
                    # to be imported right here, which made it a local of
                    # do_POST for the whole function body — so a second
                    # use further down was an UnboundLocalError until this
                    # branch happened to run first. Measured: the settings
                    # POST answered with a closed connection.
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
                _sv = (params.get("status_view") or ["table"])[0].strip().lower()
                if _sv in ("table", "list"):
                    config.status_view = _sv
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
                if "web_username" in params:
                    config.web_username = params["web_username"][0].strip()
                # Written out rather than looped over a table of names:
                # `test_form_nesting` scans this handler for the fields it
                # reads, and a name assembled at runtime is invisible to
                # it — which is exactly the gap that made a page appear to
                # send fields nothing was reading.
                if "web_session_hours" in params and params["web_session_hours"][0].strip():
                    try:
                        config.web_session_hours = max(
                            1, min(int(params["web_session_hours"][0]), 720))
                    except ValueError:
                        pass
                if "web_session_max_days" in params and params["web_session_max_days"][0].strip():
                    try:
                        config.web_session_max_days = max(
                            1, min(int(params["web_session_max_days"][0]), 365))
                    except ValueError:
                        pass
                if "web_password" in params:
                    new_pw = params["web_password"][0]
                    if new_pw:
                        # Hashed on the way in (#60). What gets written to
                        # settings.json is scrypt, never the password —
                        # `webauth.verify` still accepts a plaintext one so
                        # that WEB_PASSWORD, which can only ever be
                        # plaintext, keeps working.
                        config.web_password = webauth.hash_password(new_pw)
                        # Every existing session belonged to the old
                        # password. Changing it because you think someone
                        # else has it, and leaving their browser signed in,
                        # would defeat the point of changing it.
                        #
                        # NOT named `store`: `create_handler` takes a
                        # `store` argument and a local of that name
                        # shadows it for the whole of do_POST, which is
                        # 900 lines long and uses it further down. Same
                        # trap the `quote` import fell into.
                        sessions = self._sessions()
                        if sessions is not None:
                            sessions.clear()

                # Persist all changes
                config.save_persistent()

                self._send_redirect("/settings?saved=1")
            elif path == "/connections":
                # The notification channels, moved off the Settings page
                # when the Discord bot made its Channels tab the longest
                # thing on it (#57). Same shape as /settings above and
                # deliberately so: validate everything first, write once,
                # then act on anything that needs a running service told.
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                # keep_blank_values, or no field here can ever be
                # emptied — parse_qs drops `name=` with an empty value,
                # which is exactly the submission that means "clear it".
                params = parse_qs(body, keep_blank_values=True)

                errors = []
                if "discord_webhook" in params:
                    ok, err = _validate_webhook_url(
                        params["discord_webhook"][0].strip(), kind="discord")
                    if not ok:
                        errors.append(f"Discord webhook: {err}")
                if "webhook_url" in params:
                    ok, err = _validate_webhook_url(
                        params["webhook_url"][0].strip(), kind="generic")
                    if not ok:
                        errors.append(f"Webhook URL: {err}")
                if errors:
                    self._send_redirect(
                        "/connections?error=" + quote(" | ".join(errors)))
                    return

                if "discord_webhook" in params:
                    config.discord_webhook = params["discord_webhook"][0].strip()
                if "webhook_url" in params:
                    config.webhook_url = params["webhook_url"][0].strip()
                if "telegram_topic_id" in params:
                    config.telegram_topic_id = params["telegram_topic_id"][0].strip()
                # Empty input clears the list (= "any user in the
                # configured chat").
                if "telegram_allowed_users" in params:
                    raw = params["telegram_allowed_users"][0]
                    config.telegram_allowed_users = [
                        u.strip() for u in raw.split(",") if u.strip()
                    ]
                if "bot_label" in params:
                    # Cap at 32 chars — Telegram message length isn't a
                    # concern at that size but a runaway label would be
                    # cosmetic noise on every notification.
                    config.bot_label = params["bot_label"][0].strip()[:32]

                # ── E-mail ───────────────────────────────────────────
                if "smtp_host" in params:
                    config.smtp_host = params["smtp_host"][0].strip()
                if "smtp_port" in params:
                    try:
                        v = int(params["smtp_port"][0].strip())
                        config.smtp_port = max(1, min(v, 65535))
                    except (ValueError, IndexError):
                        pass
                if "smtp_user" in params:
                    config.smtp_user = params["smtp_user"][0].strip()
                # Not stripped, matching from_env() and DOCKER_PASSWORD:
                # a password may legitimately start or end with a space.
                # Empty means "unchanged", same as every other secret on
                # this page; the checkbox is what removes it.
                if "smtp_password" in params:
                    new_pw = params["smtp_password"][0]
                    if new_pw:
                        config.smtp_password = new_pw
                if "smtp_password_clear" in params:
                    config.smtp_password = ""
                if "smtp_from" in params:
                    config.smtp_from = params["smtp_from"][0].strip()
                if "smtp_to" in params:
                    config.smtp_to = params["smtp_to"][0].strip()
                if "smtp_tls" in params:
                    cand = params["smtp_tls"][0].strip().lower()
                    if cand in ("starttls", "ssl", "none"):
                        config.smtp_tls = cand
                # Checkbox semantics only for a submission that really came
                # from this form — `conn_page` is the hidden marker. An
                # unchecked box submits nothing, so absence means "off",
                # and for the flag that decides whether the SMTP password
                # is handed to an unverified certificate, "off because
                # somebody POSTed something else to this path" is not a
                # failure mode worth having.
                if "conn_page" in params:
                    config.smtp_tls_verify = "smtp_tls_verify" in params
                    # Same form-marker rule: only a submission that really
                    # came from this page may read an absent checkbox as
                    # "off".
                    config.discord_public_replies = (
                        "discord_public_replies" in params)
                    # The channel switches, same rule and for the same
                    # reason: an unchecked box submits nothing, so only a
                    # submission that really came from this whole form
                    # may read absence as "off".
                    #
                    # A switch is only rendered for a channel that is
                    # complete, so an incomplete one is absent from the
                    # form and must keep whatever it had — otherwise
                    # filling in a half-configured channel would silently
                    # switch it off at the same moment it became usable.
                    for _flag in ("channel_discord_enabled",
                                  "channel_webhook_enabled",
                                  "channel_smtp_enabled",
                                  "channel_ntfy_enabled",
                                  "channel_gotify_enabled",
                                  "channel_matrix_enabled",
                                  "channel_apprise_enabled",
                                  "channel_telegram_enabled",
                                  "channel_discordbot_enabled"):
                        if f"{_flag}_shown" in params:
                            setattr(config, _flag, _flag in params)

                # ── ntfy / Gotify / Matrix / Apprise ─────────────────
                # Plain values first, then the credentials, which follow
                # the rule every secret on this page follows: empty means
                # "leave it alone", and `<name>_clear` is what removes it.
                if "discord_bot_channel" in params:
                    config.discord_bot_channel = params["discord_bot_channel"][0].strip()
                for _plain in ("ntfy_url", "ntfy_server", "ntfy_topic",
                               "ntfy_user", "gotify_url", "matrix_homeserver",
                               "matrix_room", "apprise_url", "apprise_tag"):
                    if _plain in params:
                        setattr(config, _plain, params[_plain][0].strip())
                # Distinct loop variable on purpose: both loops using the
                # same name made the `f"{var}_clear"` companions
                # indistinguishable, and the symmetry check invented a
                # `_clear` for every plain field.
                for _key in ("ntfy_token", "ntfy_password", "gotify_token",
                             "matrix_token", "apprise_urls"):
                    if _key in params:
                        # Not stripped for the password, matching every
                        # other credential here: it may end in a space.
                        _val = params[_key][0]
                        if _key != "ntfy_password":
                            _val = _val.strip()
                        if _val:
                            setattr(config, _key, _val)
                    if f"{_key}_clear" in params:
                        setattr(config, _key, "")

                # ── Interactive Discord bot (#57, @NotRetarded) ──────
                # `_discord_changed` decides afterwards whether the bot
                # has to be restarted, so saving anything else on this
                # page never bounces a working bot.
                _discord_before = (config.discord_bot_token,
                                   config.discord_app_id,
                                   config.discord_guild_id,
                                   list(config.discord_allowed_users or []))
                # The token behaves like web_password: empty means "leave
                # it as it is", never "clear it", because the field is
                # rendered blank on every page load. Clearing is an
                # explicit act — the checkbox below.
                if "discord_bot_token" in params:
                    new_token = params["discord_bot_token"][0].strip()
                    if new_token:
                        config.discord_bot_token = new_token
                if "discord_bot_token_clear" in params:
                    config.discord_bot_token = ""
                if "discord_app_id" in params:
                    config.discord_app_id = params["discord_app_id"][0].strip()
                if "discord_guild_id" in params:
                    config.discord_guild_id = params["discord_guild_id"][0].strip()
                if "discord_allowed_users" in params:
                    raw = params["discord_allowed_users"][0]
                    config.discord_allowed_users = [
                        u.strip() for u in raw.replace(";", ",").split(",")
                        if u.strip()
                    ]
                _discord_changed = _discord_before != (
                    config.discord_bot_token, config.discord_app_id,
                    config.discord_guild_id,
                    list(config.discord_allowed_users or []))

                config.save_persistent()

                # Restart AFTER the write, never before: if the bot fails
                # to come up the values still have to be on disk, or the
                # user loses what he just typed and has no way to correct
                # a typo.
                _q = "/connections?saved=1"
                if _discord_changed:
                    if restart_discord is None:
                        # No callback — a handler built outside main().
                        # Saved, but nothing here can act on it.
                        _q += "&discord=restart_needed"
                    else:
                        ok, code, detail = restart_discord()
                        if ok and not code:
                            _q += "&discord=ok"
                        else:
                            _q += "&discord=" + quote(code or "error")
                            if detail:
                                _q += "&discord_detail=" + quote(detail[:200])
                self._send_redirect(_q)
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
                    # The same `lifecycle.act` both chats use, so the Web
                    # UI cannot drift from them — and so it gets the two
                    # guards it never had. It only ever checked "would
                    # this stop me?"; stop-protection (#38) and "an update
                    # is running" were missing here, which meant the
                    # button could stop the VPN container the chats
                    # refuse to touch.
                    #
                    # Every host, not just the local one: `_would_kill_self`
                    # compares full container IDs through the host's own
                    # backend, so a same-named container elsewhere has a
                    # different ID and is not mistaken for us.
                    import lifecycle
                    try:
                        outcome = lifecycle.act(
                            action, [None],
                            backend_for=lambda _x: _be,
                            checker_for=lambda _x: _ck,
                            store_for=lambda _x: _st,
                            partial=name,
                            update_running=bot.update_running)
                        for _r in ((outcome.fatal,) if outcome.fatal
                                   else outcome.replies):
                            print(f"Lifecycle {action} {name}: "
                                  f"{bot.t(_r.key, **_r.params)}")
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
            elif path == "/api/test_channel":
                # "Send test" on the Connections page, for any channel.
                # Uses the SAVED settings: e-mail alone has seven fields,
                # and a test built from whatever is currently in the form
                # would answer about a configuration that exists nowhere.
                #
                # The channel is isolated first — every other channel
                # blanked in a copy of config — so the message goes to
                # the one being tested and the answer is about it. The
                # switch is overridden too: someone testing a channel
                # they have just turned off is asking whether it works,
                # not whether it is on.
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                name = parse_qs(body).get("name", [""])[0].strip().lower()

                from notifier import Notifier
                known = {p.name for p in Notifier(config)._plugins}
                if name not in known:
                    response = {"ok": False, "error": "unknown channel"}
                else:
                    probe = Notifier(Notifier(config).isolated_config(name))
                    if not probe.has_channels():
                        # Nothing to test: the channel is incomplete. Say
                        # that rather than reporting a cheerful success
                        # for a message that went nowhere.
                        response = {"ok": False, "error": "not configured"}
                    else:
                        # The channels are best-effort by contract: they
                        # log and return on failure and never raise. So a
                        # bare try/except reports success for a message
                        # that went nowhere — measured, with a deliberately
                        # wrong webhook: the endpoint answered {"ok": true}
                        # while the channel printed
                        #
                        #   Discord webhook error: HTTP 404
                        #
                        # A cheerful success for something that did not
                        # happen is the whole failure mode this button
                        # exists to prevent, so the verdict comes from
                        # what the channel actually said. That line is its
                        # designed signal; showing it in the toast just
                        # saves someone the trip to `docker logs`.
                        #
                        # redirect_stdout is process-wide, so for the
                        # second or two a test takes, another thread's
                        # output would land in this buffer instead of the
                        # log. Bounded, and only on an explicit button
                        # press — worth it to stop reporting sends that
                        # did not happen.
                        import contextlib as _ctx
                        buf = _io.StringIO()
                        try:
                            with _ctx.redirect_stdout(buf):
                                probe.send_message(
                                    f"🧪 Docksentry test message ({name})")
                            said = buf.getvalue().strip()
                        except Exception as e:
                            said = f"{type(e).__name__}: {e}"
                        last = said.splitlines()[-1].strip() if said else ""
                        if last and any(w in last.lower() for w in
                                        ("error", "failed", "refused",
                                         "timed out", "rejected")):
                            response = {"ok": False, "error": last[:200]}
                        elif last:
                            # Said something that is not a failure — the
                            # SMTP "verification is OFF" warning is the
                            # one that exists. Sent, and worth repeating.
                            response = {"ok": True, "note": last[:200]}
                        else:
                            response = {"ok": True}

                payload = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            elif path == "/api/test_webhook":
                # Settings page "Send test" buttons — verify a webhook
                # URL is reachable before saving (#2 feedback). Uses the
                # URL the user just typed (not the saved one) so they
                # can debug a new value without committing it first.
                # Superseded by /api/test_channel, which covers every
                # channel and reports what the channel actually said.
                # Kept because it is a documented endpoint and costs
                # nothing; nothing in the interface calls it any more.
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
            elif path == "/api/env_adopt":
                # Take the environment's value for one setting (#2).
                #
                # The precedence itself stays as it is — flipping it would
                # silently reset everyone who set something in the env once
                # and later changed it in the Web UI. What was missing is a
                # way back: @famewolf set DISK_WARN_AUTO_CLEANUP=true,
                # watched a saved `false` beat it, and the only remedy on
                # offer was to hand-edit settings.json inside the volume.
                #
                # The value itself never leaves Config — see
                # adopt_env_value, and the leak the env-override test
                # caught when it did.
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                key = (parse_qs(body).get("key") or [""])[0].strip()
                if key:
                    config.adopt_env_value(key)
                ref = self.headers.get("Referer", "/")
                self._send_redirect(urlparse(ref).path or "/settings")
            elif path == "/api/wizard":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body, keep_blank_values=True)

                # Password — the first step, and the one thing the wizard
                # will not let you skip past silently. Either a password
                # (hashed here, never stored in the clear) or a deliberate
                # "no password" tick. Without one of those we do NOT mark
                # setup done: the redirect gate sends the user back to the
                # wizard rather than into an accidentally-open dashboard.
                pw = (params.get("web_password") or [""])[0]
                pw2 = (params.get("web_password_confirm") or [""])[0]
                no_pw = (params.get("no_password") or [""])[0] == "1"
                if not no_pw:
                    if not pw or pw != pw2:
                        self._send_redirect("/setup?pw=1")
                        return
                    config.web_password = webauth.hash_password(pw)
                if "web_username" in params:
                    config.web_username = params["web_username"][0].strip()

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
                # One implementation, two callers: Telegram restores from
                # a file dropped into the chat and needs exactly this.
                import backup as _backup
                restored, errors, dropped_links = _backup.restore(
                    bundle, config, store, PERSISTENT_KEYS)
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
            # Set when the server bounced a submit for a missing/mismatched
            # password — the JS gate is not the last word, the server is.
            pw_error = ("pw=1" in (self.path.split("?", 1)[1]
                                   if "?" in self.path else ""))

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
<span class="wstep-bar"></span>
<span class="wstep" data-step="5">5</span>
</div>

<form method="POST" action="/api/wizard">

<!-- ── Step 1: Password ──────────────────────────────────── -->
<div class="wstep-pane is-active" data-step-pane="1">
<h3 style="font-size:15px;color:var(--accent);margin-bottom:8px">🔒 {t("web_setup_pw_title")}</h3>
<p class="form-help" style="margin:0 0 12px 0">{t("web_setup_pw_intro")}</p>
{'<p class="login-error">' + _e(t("web_setup_pw_required")) + '</p>' if pw_error else ''}
<label>{t("web_setup_pw_username")}</label>
<input type="text" name="web_username" id="wiz-user" autocomplete="username" value="{_e(getattr(config, 'web_username', ''))}">
<label>{t("web_setup_pw_password")}</label>
<input type="password" name="web_password" id="wiz-pw" autocomplete="new-password">
<label>{t("web_setup_pw_confirm")}</label>
<input type="password" name="web_password_confirm" id="wiz-pw2" autocomplete="new-password">
<p class="form-help" id="wiz-pw-msg" style="color:var(--danger);margin:0 0 8px 0;display:none">{t("web_setup_pw_mismatch")}</p>
<label class="wizard-radio-row" style="margin-top:4px">
  <input type="checkbox" name="no_password" id="wiz-nopw" value="1">
  <span class="form-help" style="margin:0">{t("web_setup_pw_none")}</span>
</label>

<!-- Coming back from a backup is not the same journey as setting up
     for the first time, and it was buried five steps behind one.
     @famewolf, #2: "Why force the user to go through the setup wizard
     if they plan to import a backup? It should be on the first page as
     an option to skip the click-through's." He is right — by the time
     you are restoring, you are usually having a bad day already.
     The file carries web_setup_done, so a successful restore ends the
     wizard as well as filling it in. -->
<hr class="section-divider" style="margin:18px 0 12px">
<p class="form-help" style="margin:0 0 8px 0">{t("web_setup_restore_intro")}</p>
<button type="button" class="btn btn-outline btn-sm" onclick="document.getElementById('wiz-restore-file').click()">⬆ {t("web_setup_restore_btn")}</button>
<input type="file" id="wiz-restore-file" accept=".json,application/json" style="display:none" onchange="dsBackupImport(this)">
</div>

<!-- ── Step 2: Language ──────────────────────────────────── -->
<div class="wstep-pane" data-step-pane="2">
<h3 style="font-size:15px;color:var(--accent);margin-bottom:8px">🌐 {t("web_setup_lang_title")}</h3>
<p class="form-help" style="margin:0 0 12px 0">{t("web_setup_lang_intro")}</p>
<select name="language">{lang_options}</select>
</div>

<!-- ── Step 3: Schedule ──────────────────────────────────── -->
<div class="wstep-pane" data-step-pane="3">
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

<!-- ── Step 4: Channels ──────────────────────────────────── -->
<div class="wstep-pane" data-step-pane="4">
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

<!-- ── Step 5: Auto-update behavior ──────────────────────── -->
<div class="wstep-pane" data-step-pane="5">
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
<button type="button" class="btn" id="wizard-next">{t("web_setup_next")} →</button>
<button type="submit" class="btn" id="wizard-finish" style="display:none">✓ {t("web_setup_finish")}</button>
</div>
</form>
</div>

<script>
(function() {{
    const TOTAL = 5;
    let cur = 1;
    const steps = document.querySelectorAll('.wstep');
    const panes = document.querySelectorAll('.wstep-pane');
    const back = document.getElementById('wizard-back');
    const next = document.getElementById('wizard-next');
    const finish = document.getElementById('wizard-finish');
    const pw = document.getElementById('wiz-pw');
    const pw2 = document.getElementById('wiz-pw2');
    const nopw = document.getElementById('wiz-nopw');
    const pwMsg = document.getElementById('wiz-pw-msg');

    // Step 1 is the password, and it is a gate: you leave it either with
    // a password (typed twice, matching) or by deliberately ticking the
    // no-password box. Enforced again on the server, this is only so the
    // user is not let past a mistake without seeing it.
    function passwordStepOK() {{
        if (nopw.checked) return true;
        if (!pw.value) return false;
        if (pw.value !== pw2.value) {{ pwMsg.style.display = 'block'; return false; }}
        pwMsg.style.display = 'none';
        return true;
    }}
    nopw.addEventListener('change', () => {{
        pw.disabled = pw2.disabled = nopw.checked;
        if (nopw.checked) pwMsg.style.display = 'none';
    }});

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
    next.addEventListener('click', () => {{
        if (cur === 1 && !passwordStepOK()) return;
        if (cur < TOTAL) {{ cur++; render(); }}
    }});

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

        def _status_view(self, host_name, hstore, containers, own_name,
                         host_backend=None):
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
                # The host's own backend, so a caller that needs to ask it
                # something the view does not already carry can — the
                # leftover-backup notice needs a `ps -a`, because a
                # leftover backup is stopped and `containers` is only what
                # is running.
                "backend": host_backend,
                "containers": containers,
                "own_name": own_name,
                "pending": pending,
                "pending_names": [u["name"] for u in pending],
                # What is being updated on this host at this very second,
                # `{name: target version}`. Read once per view like every
                # other lookup here, so the table, the cards and the V2
                # list all render from the same answer.
                "updating": self._updating_now(host_name),
                "pinned": hstore.get_pinned(),
                "auto_list": hstore.get_autoupdate(),
                "ask_major": hstore.get_ask_before_major(),
                # "newer version exists" notes for pinned tags (#33) —
                # advisory, never a pending update.
                "advisories": (checker.read_advisories()
                               if hasattr(checker, "read_advisories") else {}),
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
            updating = view.get("updating") or {}
            pinned = view["pinned"]
            auto_list = view["auto_list"]
            ask_major = view["ask_major"]
            groups_lookup = view["groups"]
            notes_lookup = view["notes"]
            advisories = view.get("advisories") or {}
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
            # Monitor-only: watched and reported, never updated (#55,
            # @LeeNX — podman quadlets, where systemd owns the container).
            # Every action that would CHANGE the container is disabled;
            # the check button stays live, because knowing an update
            # exists is the entire point of still watching it.
            _monitor_only = False
            try:
                _monitor_only = checker.is_monitor_only(c["name"], c.get("labels"))
            except Exception:
                pass
            _mo_off = ' disabled' if _monitor_only else ''
            _mo_title = t("web_monitor_only_tt") if _monitor_only else ''
            _mo_mark = (f'<span class="label-mark" title="{_e(_mo_title)}">👁</span>'
                        if _monitor_only else '')
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
            # Same badge slot, two different facts. "update" is an offer;
            # once the engine has actually claimed this container the badge
            # says so, and where it is going (#2, @LeeNX — "I did get
            # confused when the logs said updates were in progress, but the
            # label was still indicating there was an update"). The running
            # state wins, because it is the newer of the two.
            if c["name"] in updating:
                badges += (f' <span class="badge badge-yellow is-updating" '
                           f'title="{_e(t("web_badge_updating_tt"))}">'
                           f'{_e(_updating_label(t, updating[c["name"]]))}</span>')
            elif c["name"] in pending_names:
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
            # A pinned version tag never reports an update, because its
            # digest never moves — so "up to date" is true and misleading
            # at once (#33, @LeeNX). Says what exists; offers nothing,
            # because the container is running what it was told to.
            _adv = advisories.get(c["name"])
            if _adv and c["name"] not in pending_names:
                badges += (f' <span class="badge badge-purple" '
                           f'title="{_e(t("web_badge_newer_tt", current=_adv.get("current",""), newer=_adv.get("newer","")))}">'
                           f'↑ {_e(_adv.get("newer",""))}</span>')
            # Podman runs its own updater. A container labelled
            # `io.containers.autoupdate` is claimed by `podman
            # auto-update` on a systemd timer, so both it and Docksentry
            # have an opinion about that container and the outcome
            # depends on which fires first. Same treatment as a quadlet
            # (#55): say so, and leave the decision where it belongs.
            # Free to check — the row already carries its labels.
            _pod_au = (c.get("labels") or {}).get("io.containers.autoupdate")
            if _pod_au:
                badges += (f' <span class="badge badge-yellow" '
                           f'title="{_e(t("web_badge_podman_auto_tt", mode=_pod_au))}">'
                           f'podman auto-update</span>')
            if c["name"] in ask_major:
                badges += f' <span class="badge badge-blue" title="{_e(t("web_badge_major_tt"))}">{_ICONS["ask"]}</span>'
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
            # Dead while its own update runs — pressing it would only earn
            # an "already running", and a live button beside a badge that
            # says "updating" invites exactly the second click.
            _running = c["name"] in updating
            _run_off = ' disabled' if _running else ''
            _run_title = (_updating_label(t, updating[c["name"]]) if _running
                          else t("web_update_tt"))
            update_btn = (
                f'<form method="POST" action="/api/update" class="inline-form">'
                f'<input type="hidden" name="name" value="{key_attr}">'
                f'<button type="submit"{_mo_off or _run_off} class="btn-icon is-active" '
                f'title="{_e(_mo_title or _run_title)}">{_ICONS["refresh"]}</button>'
                f'</form>'
            ) if (c["name"] in pending_names or _running) else ''
            pin_form_action = "/api/unpin" if is_pinned_c else "/api/pin"
            _pin_disabled = ' disabled' if (_lab_pin is not None or _monitor_only) else ''
            # Monitor-only wins the tooltip, as it does on the auto toggle:
            # it is the reason the button is dead, and "pin this container"
            # on a button that cannot be clicked explains nothing (#55,
            # @LeeNX). He reported it on pin; the same omission was on
            # restart, stop and the major-confirm toggle.
            _pin_title = (_mo_title if _monitor_only
                          else (t("web_label_authoritative") if _lab_pin is not None
                                else (t("web_unpin_tt") if is_pinned_c else t("web_pin_tt"))))
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
                _auto_disabled = (' disabled' if (_lab_auto is not None
                                                  or _monitor_only) else '')
                _auto_title = (_mo_title if _monitor_only
                               else (t("web_label_authoritative") if _lab_auto is not None
                                     else (t("web_autoupdate_disable") if is_auto
                                           else t("web_autoupdate_enable"))))
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
                f'<button type="submit"{_mo_off} class="btn-icon{" is-warn" if is_askm else ""}" '
                f'title="{_e(_mo_title or (t("web_ask_major_off") if is_askm else t("web_ask_major_on")))}">{_ICONS["ask"]}</button>'
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
                    f'<button type="submit"{_mo_off} class="btn-icon" title="{_e(_mo_title or t("web_restart_tt"))}">{_ICONS["restart"]}</button>'
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
                        f'<button type="submit"{_mo_off} class="btn-icon" title="{_e(_mo_title or t("web_stop_tt"))}">{_ICONS["x"]}</button>'
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
            cb_html = (f'<input type="checkbox" class="bulk-cb" '
                       f'value="{key_attr}" '
                       f'data-pending="{1 if c["name"] in pending_names else 0}" '
                       f'data-pinned="{1 if is_pinned_c else 0}" '
                       f'data-auto="{1 if is_auto else 0}">')
            row = f"""{row_open}
<td>{cb_html}</td>
<td>{name_cell}{_mo_mark}{badges}</td>{host_td}
<td class="image-cell"><code>{_e(c['image'])}</code> {version_html}</td>
<td>{status_badge}</td>
<td>{auto_cell}</td>
<td class="actions-cell">{actions}</td>
</tr>"""

            # The same container as a card, for narrow screens. Built here,
            # from the same locals as the row above, rather than in a second
            # function — a table row and a card that drift apart is exactly
            # the failure this project has already had twice (the Web UI's
            # own copy of the link chain, then the container page's copy of
            # the label rules). One place computes the state; two places
            # only lay it out.
            host_line = (f'<span class="tile-host">{_e(host_name)}</span> · '
                         if multi else '')
            tile = f"""<div class="tile"{f' data-host="{_e(host_name)}"' if multi else ''}>
  <div class="tile-head">{cb_html}<span class="tile-name">{name_cell}</span>{_mo_mark}{status_badge}</div>
  <div class="tile-img"><code>{_e(c['image'])}</code> {version_html}</div>
  <div class="tile-meta">{host_line}{badges}{auto_cell}</div>
  <div class="tile-actions">{actions}</div>
</div>"""
            return row, tile

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

            views = self._host_views(multi, _store_for, own_name)

            # V2 renders from JSON in the browser, so the server's job
            # here ends with the same `views` the old table is built
            # from — one reader of the store, not two that can disagree
            # about what is pinned or pending.
            if getattr(config, "status_view", "table") == "list":
                import web_v2
                from version import VERSION as _V
                self._v2_views = views
                self._send_html(self._render_page(
                    web_v2.shell(t, _V, config.language), "status", wide=False))
                return

            rows = ""
            def _ctx_hint(view):
                """The Docker contexts this machine knows, for a host we
                could not reach. Only when there is something to say, and
                only naming the ones whose endpoint is not already the one
                that just failed — repeating it back would be noise."""
                ctxs = [(n, e) for n, e in (view.get("contexts") or [])
                        if e and e != view.get("endpoint") and n != "default"]
                if not ctxs:
                    return ""
                shown = ", ".join(f"{n} ({e})" for n, e in ctxs[:4])
                return (f'<br><span style="font-size:12px">'
                        f'{_e(t("web_host_contexts", list=shown))}</span>')

            def _why_hint(view):
                """What the CLI said, verbatim, under the "unreachable" line.

                Escaped and dropped into a `<code>` because it is a command's
                output and reads as one — it contains the endpoint, quotes
                and angle-free but URL-ish text. Empty when there is nothing
                to quote, which is what the old, reasonless rows become.
                """
                why = (view.get("reason") or "").strip()
                if not why:
                    return ""
                return (f'<br><span style="font-size:12px">'
                        f'{_e(t("web_host_reason"))} <code>{_e(why)}</code>'
                        f'</span>')

            tiles = ""
            for view in views:
                if view.get("unreachable"):
                    rows += (
                        f'<tr class="host-unreachable" '
                        f'data-host="{_e(view["unreachable"])}">'
                        f'<td colspan="{host_cols}" class="muted">'
                        f'{_e(t("web_host_unreachable", host=view["unreachable"]))}'
                        f'{_why_hint(view)}{_ctx_hint(view)}</td>'
                        f'</tr>'
                    )
                    continue
                for c in view["containers"]:
                    _row, _tile = self._status_row(c, view, t, multi)
                    rows += _row
                    tiles += _tile

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
            # ── leftover update backups ─────────────────────────
            # Every update renames the running container to
            # `<name>_old` before creating the replacement, and drops it
            # once the new one is healthy. A run whose process died in
            # between left one behind for good: nothing ever removed
            # them — `cleanup_images` prunes images and
            # `_prune_old_backups` deletes backup directories on disk.
            # @LeeNX found three and reasonably concluded his containers
            # were not updating (#56).
            #
            # Fixed going forward, but the ones already lying around are
            # still there, and they are visible in the table above with
            # no explanation of what they are. So: say it. Only where
            # the live container exists too — that is the proof the swap
            # finished — and only ever as a sentence. Removing a
            # container we did not create in this run is the user's
            # call, not ours, which is why this offers a command and not
            # a button.
            # Per host, not across them: a `foo_old` on one machine and a
            # `foo` on another are two unrelated containers, and pairing
            # them would accuse an innocent one of being our debris.
            # A `ps -a` per host, because a leftover backup is STOPPED and
            # the table above lists only what is running — the first
            # version of this looked for them among the running
            # containers and could therefore never have found one. One
            # extra `ps` per host per render; measured at 42 ms against
            # the demo instance (min 36, max 50 over five runs), on a
            # page that already shells out several times.
            # Failure is silent: a host that cannot answer costs its own
            # notice, not the page.
            leftovers = []
            for _v in views:
                _live = {c["name"] for c in (_v.get("containers") or ())}
                _be = _v.get("backend")
                if _be is None or not _live:
                    continue
                try:
                    _r = _be.ps(all=True, fmt="{{.Names}}", timeout=10)
                    if getattr(_r, "returncode", 1) != 0:
                        continue
                    _all = {n for n in (_r.stdout or "").split() if n}
                except Exception:
                    continue
                # Per host, and only where the live container is present:
                # that is the proof the swap finished. A `foo_old` on one
                # machine and a `foo` on another are unrelated.
                # …and not while its update is still running. During the
                # swap BOTH exist — the new container is already up and
                # the old one is still the rollback copy — so this fired
                # mid-update and called a live safety net "left behind
                # from an interrupted update", with a `docker rm` next to
                # it. Following that advice removes the one thing a
                # failed update would have fallen back to.
                _busy = self._updating_now(_v.get("host") or "")
                leftovers += [n for n in _all
                              if n.endswith("_old") and n[:-4] in _live
                              and n[:-4] not in _busy]
            leftovers = sorted(set(leftovers))
            leftover_banner = ""
            if leftovers:
                leftover_banner = f"""<div class="card card-warn">
<h2>{_ICONS["alert"]} {t("web_leftover_title")}</h2>
<p class="card-intro">{t("web_leftover_intro", count=len(leftovers))}</p>
<pre>docker rm {_e(" ".join(leftovers))}</pre>
</div>"""

            if rows_mp:
                major_banner = f"""<div class="card card-warn">
<h2>{_ICONS["alert"]} {t("web_major_pending_title")}</h2>
<p class="card-intro">{t("web_major_pending_intro")}</p>
<div class="table-scroll"><table>{rows_mp}</table></div>
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

            # Host memory, next to host storage. @NotRetarded asked for this
            # in #2 while chasing a container that kept dying: he could see
            # disk at a glance but had to leave Docksentry to find out
            # whether the machine was under memory pressure at all — which
            # is the question that comes BEFORE "which container did it".
            #
            # `/proc/meminfo` inside a container reports the HOST's figures,
            # which is what is wanted here. Local only: that file describes
            # the machine Docksentry runs on, so showing it on a page
            # scoped to a remote host would be a plain lie.
            mem_stat = ""
            try:
                from monitor import ContainerMonitor
                mem = ContainerMonitor.host_memory_parts()
                if mem:
                    used_gb, total_gb, pct = mem
                    mem_color = "var(--danger)" if pct >= 90 else (
                        "var(--warn)" if pct >= 80 else "var(--accent)")
                    mem_stat = f"""<div class="card stat">
    <div class="num" style="color:{mem_color}">{pct:.0f}%</div>
    <div class="label">{t("web_stat_memory_usage", free=f"{total_gb - used_gb:.1f}G")}</div>
</div>"""
            except Exception:
                mem_stat = ""

            content = f"""
{leftover_banner}{major_banner}
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
{mem_stat}
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
<div class="table-scroll"><table id="ctbl">
<thead><tr><th><input type="checkbox" id="bulkSelectAll" style="width:auto" title="{t("web_bulk_select_all")}"></th><th class="sortable" onclick="sortByName()" title="{t('web_sort_name')}" style="cursor:pointer;user-select:none">{t("web_name")} <span id="nameSortArrow"></span></th>{host_th}<th>{t("web_image")}</th><th>{t("web_status")}</th><th>{t("web_autoupdate_badge")}</th><th>{t("web_actions")}</th></tr></thead>
<tbody id="ctblBody">
{rows}
</tbody>
</table></div>
<!-- The same containers as cards, for narrow screens. Both are rendered
     and CSS shows one; the alternative — deciding server-side from a
     user-agent string — guesses at a viewport it cannot see, and gets it
     wrong on a tablet held sideways. The markup cost is a few KB.
     The tile-list is a SIBLING of `.table-scroll`, not a child: on mobile
     `.table-scroll` is `display:none`, and a tile-list nested inside it
     inherited that and vanished — 16 containers, an empty list, and no
     table either (#63, @NotRetarded). The `</div>` above closes the
     scroll wrapper around the table ALONE. -->
<div class="tile-list" id="ctblTiles">
{tiles}
</div>
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
<span title="{_e(t("web_badge_major_tt"))}"><span class="btn-icon">{_ICONS["ask"]}</span> {_legend_word(t("web_legend_major_confirm"))}</span>
<span title="{_e(t("web_stop_tt"))}"><span class="btn-icon">{_ICONS["x"]}</span> {_legend_word(t("lifecycle_btn_stop"))}</span>
<span title="{_e(t("web_label_authoritative"))}">🏷 {_legend_word(t("web_legend_label"))}</span>
<span title="{_e(t("web_selfupdate_marker_tt"))}">⚙ {_legend_word(t("web_legend_selfupdate"))}</span>
<span title="{_e(t("web_link_open_tt"))}">🔗 {_legend_word(t("web_link_title"))}</span>
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
            # Labels outrank the stored toggles — the status table has read
            # them since #42, this page never did. Measured on a live
            # container carrying `docksentry.auto=true`: the status row
            # showed "auto 🏷" while this page showed the box UNCHECKED and
            # still clickable. Clicking it wrote to the store, changed
            # nothing because the label wins, and left the two pages
            # contradicting each other — the same "a control that shows a
            # state it does not have" defect @LeeNX reported in #51, on a
            # different page.
            _det_lab = {} if det_is_self else {
                k: _UC2.label_bool(det_labels, k)
                for k in ("auto", "ask-major", "trust-running")
            }
            if _det_lab.get("auto") is not None:
                is_auto = _det_lab["auto"]
            if _det_lab.get("ask-major") is not None:
                is_askm = _det_lab["ask-major"]
            if _det_lab.get("trust-running") is not None:
                is_trust_c = _det_lab["trust-running"]

            def _lab_lock(key):
                """`(disabled-attr, marker)` for a label-governed control.

                A locked control is disabled rather than merely marked: a
                click that silently does nothing is what this whole class
                of bug is made of.
                """
                if _det_lab.get(key) is None:
                    return "", ""
                return (' disabled',
                        f' <span class="label-mark" '
                        f'title="{_e(t("web_label_authoritative"))}">🏷</span>')
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
            # The detail page reads the LOCAL daemon, so the only claims
            # that can be about this container are the local ones.
            from container_store import LOCAL_HOST as _LH
            _updating_here = self._updating_now(_LH)
            if name in _updating_here:
                badges.append(
                    f'<span class="badge badge-yellow is-updating" '
                    f'title="{_e(t("web_badge_updating_tt"))}">'
                    f'{_e(_updating_label(t, _updating_here[name]))}</span>')
            elif pending_for_self:
                badges.append(f'<span class="badge badge-yellow">{t("web_badge_update")}</span>')
            badges_html = " ".join(badges)

            compose_row = ""
            if compose_info:
                compose_row = (
                    f'<tr><td>{t("web_detail_compose")}</td>'
                    f'<td><code>{_e(compose_info.get("compose_project",""))} / {_e(compose_info.get("compose_service",""))}</code></td></tr>'
                )
                # The path Docksentry actually opens, and whether it is
                # there. Without this the only way to find out was a
                # `docker inspect` on the label (#2).
                _paths, _ok = self._compose_reach(compose_info)
                if _paths:
                    _mark = ("badge-green", t("web_compose_file_found")) if _ok else \
                            ("badge-yellow", t("web_compose_file_missing"))
                    compose_row += (
                        f'<tr><td>{t("web_compose_file")}</td><td>'
                        + "<br>".join(f"<code>{_e(p)}</code>" for p in _paths)
                        + f' <span class="badge {_mark[0]}">{_e(_mark[1])}</span>'
                        + ("" if _ok else
                           f'<div class="form-help" style="margin:4px 0 0">{_e(t("web_compose_file_hint"))}</div>'
                           + self._compose_mount_block(t, _paths, compose_info))
                        + '</td></tr>'
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

            overview_html = f"""<div class="table-scroll"><table>
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
</table></div>
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
                history_html = f"""<div class="table-scroll"><table>
<tr><th>{t("web_date")}</th><th>{t("web_result")}</th><th>{t("web_detail")}</th></tr>
{hist_rows}
</table></div>"""
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
  <input type="checkbox" id="cb-detail-auto" {'checked' if is_auto else ''}{_lab_lock("auto")[0]} onchange="document.getElementById('frm-detail-auto').submit()">
  <label for="cb-detail-auto">{t("web_autoupdate_enable")}{_lab_lock("auto")[1]}</label>
</div>
<form id="frm-detail-auto" method="POST" action="/api/autoupdate" class="inline-form">
<input type="hidden" name="name" value="{_e(name)}">
</form>
<p class="form-help">{t("web_detail_auto_hint")}</p>
"""
            settings_html = f"""{auto_block}

<div class="form-checkbox-row">
  <input type="checkbox" id="cb-detail-major" {'checked' if is_askm else ''}{_lab_lock("ask-major")[0]} onchange="document.getElementById('frm-detail-major').submit()">
  <label for="cb-detail-major">{t("web_ask_major_on")}{_lab_lock("ask-major")[1]}</label>
</div>
<form id="frm-detail-major" method="POST" action="/api/ask_major" class="inline-form">
<input type="hidden" name="name" value="{_e(name)}">
</form>
<p class="form-help">{t("web_detail_major_hint")}</p>

<div class="form-checkbox-row">
  <input type="checkbox" id="cb-detail-trust" {'checked' if is_trust_c else ''}{_lab_lock("trust-running")[0]} onchange="document.getElementById('frm-detail-trust').submit()">
  <label for="cb-detail-trust">{t("web_trust_running")}{_lab_lock("trust-running")[1]}</label>
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

        def _compose_mount_block(self, t, paths, info):
            """The volume line to add, and the sentence that frames it.

            Two answers, and which one we can give says something: the
            daemon knows the exact mount whenever the files live inside
            another container, and that beats any advice we could word.
            Only when nothing holds them — a plain `docker compose` run,
            where the label already is a host path — is the mount the
            directory onto itself.
            """
            exact = self._compose_mount_exact(paths)
            # A mount that lands where we already have something would
            # shadow it. `/data` is the collision that matters: it is our
            # own state directory and Portainer's as well.
            clash = sorted({d for _p, line in (exact or []) if line
                            for _l, d in (line,)} & self._own_mount_dests())
            if clash:
                return (f'<div class="form-help" style="margin:4px 0 0">'
                        f'{_e(t("web_compose_mount_clash", path=clash[0]))}</div>')
            if exact:
                # One line per path: the exact one where the daemon knows
                # who holds the file, the directory onto itself where it
                # does not. Deduplicated, because two files in one place
                # need one mount.
                seen, parts = [], []
                for _p, line in exact:
                    lhs, dest = line if line else (self._compose_mount_targets(
                        [_p], info.get("compose_project", ""))[0],) * 2
                    if (lhs, dest) in seen:
                        continue
                    seen.append((lhs, dest))
                    parts.append(f'<pre class="mount-hint">volumes:\n'
                                 f'  - {_e(lhs)}:{_e(dest)}:ro</pre>')
                lines = "".join(parts)
                note = t("web_compose_mount_exact_note")
            else:
                lines = "".join(
                    f'<pre class="mount-hint">volumes:\n'
                    f'  - {_e(m)}:{_e(m)}:ro</pre>'
                    for m in self._compose_mount_targets(
                        paths, info.get("compose_project", "")))
                note = t("web_compose_mount_note")
            return (lines
                    + f'<div class="form-help" style="margin:4px 0 0">'
                      f'{_e(note)}</div>')

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
                "compose_working_dir": labels.get("com.docker.compose.project.working_dir", ""),
            }

        @staticmethod
        def _compose_reach(info):
            """`(paths, reachable)` for a container's compose files.

            Answers the question the update path asks, with the update
            path's own resolver — a page that says "reachable" while
            `docker compose up` disagrees would be worse than saying
            nothing (#2, @NotRetarded, who mounted his stacks where we
            told him to and still could not be told what was wrong).
            """
            import os as _os
            from update_checker import UpdateChecker as _UC
            raw = info.get("compose_file", "")
            if not raw:
                return [], False
            try:
                paths = _UC._compose_files(raw, info.get("compose_working_dir") or None)
            except Exception:
                paths = [raw]
            return paths, bool(paths) and all(_os.path.isfile(f) for f in paths)

        #: Container mounts, refreshed at most twice a minute. Reading every
        #: container to answer one page would be a daemon round trip per
        #: render, and mounts change about as often as containers do.
        _mounts_cache = {"t": 0.0, "rows": []}

        @staticmethod
        def _all_mounts():
            """`[{image, type, vol, src, dest}]` for every container.

            The daemon knows where a container's paths really come from —
            including the true source of a *named volume*, which is the
            case a directory mount cannot express at all. Portainer keeps
            its stacks in `portainer_data`, so "mount that directory" was
            never an instruction anyone could follow (#2).
            """
            now = time.time()
            c = WebHandler._mounts_cache
            if c["rows"] and now - c["t"] < 30:
                return c["rows"]
            rows = []
            try:
                ids = backend.run(["ps", "-aq"])
                names = ids.stdout.split() if ids.returncode == 0 else []
                if names:
                    r = backend.run(["inspect", *names])
                    for ins in (json.loads(r.stdout) if r.returncode == 0 else []):
                        img = (ins.get("Config", {}).get("Image") or "").lower()
                        for m in ins.get("Mounts") or []:
                            dest = (m.get("Destination") or "").rstrip("/")
                            if dest:
                                rows.append({"name": (ins.get("Name") or "").lstrip("/"),
                                             "image": img, "type": m.get("Type"),
                                             "vol": m.get("Name") or "",
                                             "src": m.get("Source") or "",
                                             "dest": dest})
            except Exception:
                rows = []
            c["t"], c["rows"] = now, rows
            return rows

        def _own_mount_dests(self):
            """Where WE already have something mounted.

            A suggested mount that lands on one of these would shadow it.
            Docksentry keeps its own state in `/data` and so does
            Portainer — offering `portainer_data:/data:ro` to somebody
            running both would have read-only-mounted a stranger's volume
            over our own database (#2, caught before release).
            """
            own = self._own_container_name_safe()
            if not own:
                return set()
            return {r["dest"] for r in self._all_mounts() if r.get("name") == own}

        #: Images whose name gives away a stack manager. Only used to break
        #: a tie — never to decide on its own.
        _MANAGER_HINTS = ("portainer", "dockge", "dockhand", "komodo", "yacht")

        @staticmethod
        def _compose_mount_exact(paths):
            """`[(path, (lhs, dest) or None)]`, or None when we know nothing.

            Finds the container that already holds each file and reads its
            mount straight off the daemon — so it works for a manager we
            have never heard of, as long as it runs on this machine.

            Per path, not all-or-nothing: a label can name a manager's file
            and a host-side override at once, and bailing on the whole
            thing made the caller offer to mount the container-internal
            path onto itself, which cannot work. A path nothing holds gets
            `None` and the caller fills it in with the self-mount form.

            Still `None` for the whole set when several containers mount
            the same depth and none looks like a manager: a confidently
            wrong mount is what this thread was about.
            """
            rows = WebHandler._all_mounts()
            if not rows:
                return None
            try:
                from compose_paths import owner as _owner
            except Exception:
                _owner = lambda _p: None
            out = []
            for p in paths:
                hits = [r for r in rows
                        if p == r["dest"] or p.startswith(r["dest"] + "/")]
                if not hits:
                    out.append((p, None))            # a host path; not ours
                    continue
                deepest = max(len(h["dest"]) for h in hits)
                hits = [h for h in hits if len(h["dest"]) == deepest]
                if len(hits) > 1:
                    own = (_owner(p) or "").lower()
                    looks = [h for h in hits
                             if any(k in h["image"] for k in WebHandler._MANAGER_HINTS)
                             and (not own or any(w in h["image"]
                                                 for w in own.split(" or ")))]
                    if len(looks) != 1:
                        return None                  # ambiguous: say nothing
                    hits = looks
                h = hits[0]
                out.append((p, (h["vol"] if h["type"] == "volume" else h["src"],
                                h["dest"])))
            return out if any(line for _p, line in out) else None

        @staticmethod
        def _compose_mount_targets(paths, project=""):
            """The container-side paths a mount would have to land on.

            The left half of a volume line is the user's business — only
            they know where their files live. The right half is ours, and
            it is the whole answer: it has to match the label, not their
            host layout (#2, @NotRetarded, who mounted where we told him
            and still landed nowhere).

            A stack manager we recognise gets its root, because one mount
            covers every stack it holds. Everything else gets the file's
            own directory — always right for this container, even if it
            means a mount per stack.

            Deliberately no cleverness beyond that. Walking up to a
            likely "stacks root" would have suggested mounting someone's
            entire home directory for a plain `docker compose` project,
            and a confidently wrong mount is what started this thread.
            """
            import os as _os
            try:
                from compose_paths import mount_root as _mr
            except Exception:
                _mr = lambda _p: None
            out = []
            for f in paths:
                root = _mr(f) or _os.path.dirname(f)
                if root and root not in out:
                    out.append(root)
            return out

        def _container_window_form(self, t, name, window):
            """Render the per-container update-window editor (subset of the
            global Update-Windows section, scoped to one container)."""
            # Not hardcoded English: this list is rendered next to the
            # update-window controls in whatever language the UI is set to,
            # and "Mon Tue Wed" in a German page is the same locale leak the
            # cron preview had.
            _wd = t("web_weekdays_short").split(",")
            wd_full = ([w.strip() for w in _wd] if len(_wd) == 7
                       else ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
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
<div class="table-scroll"><table>
<tr><th>{t("web_date")}</th><th>{t("web_name")}</th><th>{t("web_result")}</th><th>{t("web_detail")}</th></tr>
{rows}
</table></div>
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
                    # Who was holding memory and CPU when it died (#2,
                    # @NotRetarded). The alert has carried this since
                    # v1.65.0; the history could not, because it was
                    # gathered after the event was written and thrown away
                    # with the message. Same i18n keys as the alert, so
                    # the row and the notification cannot word it
                    # differently. Absent on older events and on kinds
                    # where it means nothing — then this is simply empty.
                    res = ev.get("resources") or {}
                    # Same two moments the alert distinguishes (#66): the
                    # top lists were taken as the container died, the
                    # victim line in the check that followed. Events from
                    # before this carry no `at` and keep the death
                    # wording — the evidence path is what they took.
                    when = "" if res.get("at", "death") == "death" else "_after"
                    extra = ""
                    for key, tkey in (("host", "monitor_host_memory"),
                                      ("load", "monitor_host_cpu"),
                                      ("victim", "monitor_victim_usage"),
                                      ("mem", "monitor_top_memory" + when),
                                      ("cpu", "monitor_top_cpu" + when)):
                        if res.get(key):
                            if key == "victim":
                                arg = {"state": res[key],
                                       "name": ev.get("container", "?")}
                            elif key in ("host", "load"):
                                arg = {"state": res[key]}
                            else:
                                arg = {"list": res[key]}
                            extra += f'<div>{_e(t(tkey, **arg))}</div>'
                    # An idle host said out loud, so a row without the CPU
                    # line cannot be mistaken for a lost measurement.
                    if not res.get("cpu") and res.get("cpu_quiet"):
                        extra += f'<div>{_e(t("monitor_top_cpu_quiet" + when, pct=res["cpu_quiet"]))}</div>'
                    if res.get("oom_flag"):
                        extra += f'<div>{_e(t("monitor_oom_flag_" + res["oom_flag"]))}</div>'
                    if extra:
                        extra = f'<div class="event-res">{extra}</div>'
                    ev_rows += f"""<tr>
<td>{_e(ev.get('timestamp', ''))}</td>
<td>{_e(ev.get('container', ''))}</td>
<td style="font-size:12px">{_e(msg)}{extra}</td>
</tr>"""
                content += f"""<div class="card">
<h2>{t("web_events")}</h2>
<div class="table-scroll"><table>
<tr><th>{t("web_date")}</th><th>{t("web_name")}</th><th>{t("web_detail")}</th></tr>
{ev_rows}
</table></div>
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

            # ── audit trail (v2.1) ──────────────────────────────
            # Beside the event log on purpose: that one says what happened
            # TO containers, this one says what people DID to them. Same
            # page answers "what went on last night" from both directions.
            audit = getattr(getattr(self, "server", None), "audit", None)
            rows = audit.entries(100) if audit is not None else []
            if rows:
                a_rows = ""
                for e in rows:
                    det = e.get("detail") or {}
                    extra = ", ".join(f"{k}={v}" for k, v in det.items()
                                      if k not in ("name", "container"))
                    a_rows += f"""<tr>
<td>{_e(e.get('timestamp',''))}</td>
<td><span class="badge badge-blue">{_e(e.get('source','?'))}</span>{'' if e.get('actor') in ('', e.get('source')) else ' ' + _e(e.get('actor'))}</td>
<td><code>{_e(e.get('action',''))}</code>{(' ' + _e(e.get('target'))) if e.get('target') else ''}</td>
<td style="font-size:11px;color:var(--text-muted)">{_e(extra)}</td>
</tr>"""
                # `id` and the untranslated word both exist so the section
                # can be FOUND: the heading is translated in all 16
                # languages and none of them contains "audit", so a browser
                # search for the term that everyone actually uses — and
                # that the docs, changelog and issues all use — turned up
                # nothing on the page that has it.
                content += f"""<div class="card" id="audit">
<h2>{t("web_audit")} <span class="h2-alt">· Audit-Trail</span></h2>
<p class="card-intro">{t("web_audit_intro")}</p>
<div class="table-scroll"><table>
<tr><th>{t("web_date")}</th><th>{t("web_audit_who")}</th><th>{t("web_audit_what")}</th><th>{t("web_detail")}</th></tr>
{a_rows}
</table></div>
</div>"""

            self._send_html(self._render_page(content, "history"))

        # ── shared by the Settings and Connections pages ──────────
        # Both were local closures inside _page_settings until the
        # channels moved to a page of their own. Two copies of the env
        # marker is exactly how one page ends up silently not warning
        # about an overruled variable.
        def _api_token_card(self, t):
            """Read-only card for `API_TOKENS`.

            The one setting where the interface said nothing at all — not
            the values, which is right, but not even whether any exist.
            "Is the scraper authorised, or is it getting 401s?" was a
            question you could only answer by reading the compose file and
            then the container's logs.

            So: the names, and when each was last seen. Never the tokens —
            they are shown once, in the file the operator wrote them in,
            and this page is reachable by anyone holding the Web UI
            password, which is a *different* secret on purpose. Not a form
            either: the values live in the environment, and a field that
            silently fails to save is worse than no field.
            """
            configured = getattr(config, "api_tokens", []) or []
            seen = getattr(getattr(self, "server", None), "token_seen", {}) or {}
            # `api_tokens` is a persistent key, so a saved empty list beats
            # a set `API_TOKENS` — and then the variable is in the compose
            # file, plainly set, doing nothing. Found while testing this
            # very card: the tokens were in the environment and every
            # request was refused, with no way to see why. Silence is the
            # wrong answer for a setting that is being overruled.
            overruled = ""
            try:
                if config.env_override("api_tokens"):
                    overruled = (f'<p class="card-intro">'
                                 f'<span class="badge badge-yellow">env</span> '
                                 f'{_e(t("web_api_tokens_overruled"))}</p>')
            except Exception:                                # pragma: no cover
                overruled = ""
            if not configured:
                return f"""<div class="card">
<h2>{t("web_api_tokens")}</h2>
<p class="card-intro">{t("web_api_tokens_intro")}</p>
{overruled}
<p class="card-intro">{t("web_api_tokens_none")}</p>
</div>
"""
            rows = ""
            for entry in configured:
                name, _, token = str(entry).partition(":")
                label = name.strip() or "token"
                if not token.strip():
                    # `API_TOKENS=prom` — no secret at all. It can never
                    # match, so it is not protecting anything; saying so
                    # here beats leaving someone to wonder why their
                    # scraper is refused.
                    when = (f'<span class="badge badge-yellow">'
                            f'{_e(t("web_api_tokens_broken"))}</span>')
                else:
                    ts = seen.get(label)
                    if ts:
                        from datetime import datetime as _dt
                        # Local time, same format as the audit table right
                        # next door — two clocks on one page is its own
                        # small confusion.
                        when = _e(_dt.fromtimestamp(ts)
                                  .strftime("%Y-%m-%d %H:%M:%S"))
                    else:
                        when = (f'<span style="color:var(--text-muted)">'
                                f'{_e(t("web_api_tokens_never"))}</span>')
                rows += (f"<tr><td><code>{_e(label)}</code></td>"
                         f"<td>{when}</td></tr>")
            return f"""<div class="card">
<h2>{t("web_api_tokens")}</h2>
<p class="card-intro">{t("web_api_tokens_intro")}</p>
{overruled}
<div class="table-scroll"><table>
<tr><th>{t("web_api_tokens_name")}</th><th>{t("web_api_tokens_last")}</th></tr>
{rows}
</table></div>
<p class="card-intro" style="margin:10px 0 0">{t("web_api_tokens_hint")}</p>
</div>
"""

        def _help(self, text):
            return f'<span class="help" data-tt="{_e(text)}">?</span>'

        def _env_mark(self, key, t):
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

            help_ = self._help
            env_ = lambda key: self._env_mark(key, t)

            content = f"""
<div class="card">
<h2>{t("web_settings")}</h2>
<p class="card-intro">{t("web_settings_intro")}</p>

<form method="POST" action="/settings" id="settings-form"></form>
<div class="tabs" data-tabs="settings">
  <button type="button" class="tab-btn" data-tab-target="general">{_ICONS["settings"]}<span>{t("web_tab_general")}</span></button>
  <button type="button" class="tab-btn" data-tab-target="updates">{_ICONS["refresh"]}<span>{t("web_tab_updates")}</span></button>
  <button type="button" class="tab-btn" data-tab-target="cleanup">{_ICONS["broom"]}<span>{t("web_tab_cleanup")}</span></button>
  <button type="button" class="tab-btn" data-tab-target="notifs">{_ICONS["alert"]}<span>{t("web_tab_notifications")}</span></button>
</div>

<!-- ── Allgemein ─────────────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="general">
  <div class="grid">
    <div>
      <label>{t("web_status_view")} {help_(t("web_status_view_hint"))}{env_("status_view")}</label>
  <select name="status_view" form="settings-form">
    <option value="table" {'selected' if getattr(config, "status_view", "table") != "list" else ''}>{t("web_status_view_table")}</option>
    <option value="list" {'selected' if getattr(config, "status_view", "table") == "list" else ''}>{t("web_status_view_list")}</option>
  </select>
  <label>{t("web_language")}{env_("language")}</label>
      <select name="language" form="settings-form">{lang_options}</select>
    </div>
    <div>
      <label>{t("web_cron_schedule")} {help_(t("web_cron_help"))}{env_("cron_schedule")}</label>
      <input type="text" name="cron_schedule" id="f-cron_schedule" value="{_e(config.cron_schedule)}" oninput="dsCronPreview()" form="settings-form">
      <div id="cron-preview" style="font-size:11px;color:var(--muted);margin-top:4px;min-height:14px">⏳ {_e(t("web_cron_preview_loading"))}</div>
    </div>
  </div>
  <label>{t("web_excluded")} {help_(t("web_excluded_help"))}{env_("exclude_containers")}</label>
  <input type="text" name="exclude_containers" value="{_e(', '.join(config.exclude_containers))}" placeholder="container1, container2" form="settings-form">
  <!-- Web UI password. The stored value is NEVER rendered back into the
       field (it is a secret, not on LOGGABLE_PERSISTENT_KEYS) — the box
       always starts empty and an empty submit means "leave unchanged". -->
  <label>{t("web_password_label")} {help_(t("web_password_hint"))}{env_("web_password")}</label>
  <input type="password" name="web_password" value="" placeholder="{_e(t('web_password_placeholder'))}" autocomplete="new-password" form="settings-form">
  <p class="form-help">{t("web_password_hashed")}</p>

  <label>{t("web_username")} {help_(t("web_username_help"))}{env_("web_username")}</label>
  <input type="text" name="web_username" value="{_e(getattr(config, 'web_username', ''))}" autocomplete="username" form="settings-form">

  <div class="adv-only">
    <label>{t("web_session_hours")} {help_(t("web_session_hours_help"))}{env_("web_session_hours")}</label>
    <input type="number" name="web_session_hours" min="1" max="720" value="{_e(getattr(config, 'web_session_hours', 8))}" form="settings-form">

    <label>{t("web_session_max_days")} {help_(t("web_session_max_days_help"))}{env_("web_session_max_days")}</label>
    <input type="number" name="web_session_max_days" min="1" max="365" value="{_e(getattr(config, 'web_session_max_days', 7))}" form="settings-form">
  </div>
  <!-- NOT adv-only. It used to be, and that closed the last door: DEBUG
       from the environment can be overruled by settings.json, and the
       only other way to switch debug on was a checkbox that simple mode
       hides with display:none — invisible even to the browser's own
       find-in-page. Env powerless, switch unfindable, no way in at all
       (#53, @LeeNX). One toggle in the simple view is the smaller cost. -->
  <div class="form-checkbox-row">
    <input type="checkbox" name="debug" id="cb-debug" {cb(config.debug)} form="settings-form">
    <label for="cb-debug">{t("web_debug_mode")} {help_(t("web_debug_help"))}{env_("debug")}</label>
  </div>
<!-- In General. These cards used to sit outside all five panes,
     so switching tabs never changed them and they read as repeated
     on every one (#2, @NotRetarded). -->
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
<!-- In General, with the rest of the read-only information. -->
<div class="card">
<h2>Info</h2>
<div class="table-scroll"><table>
<tr><td>Version</td><td><code>v{_e(VERSION)}</code></td></tr>
<tr><td>Telegram</td><td><code>{telegram_status}</code></td></tr>
<tr><td>Bot Token</td><td><code>{_e(token_masked)}</code></td></tr>
<tr><td>Chat ID</td><td><code>{_e(chat_masked)}</code></td></tr>
<tr><td>Data Dir</td><td><code>{_e(config.data_dir)}</code></td></tr>
</table></div>
<p class="form-help" style="margin-top:8px">{t("web_info_credentials_hint")}</p>
</div>
</div>

<!-- ── Updates ────────────────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="updates">
  <div class="form-checkbox-row">
    <input type="checkbox" name="auto_selfupdate" id="cb-auto-su" {cb(config.auto_selfupdate)} form="settings-form">
    <label for="cb-auto-su">{t("web_auto_selfupdate")} {help_(t("web_auto_selfupdate_help"))}{env_("auto_selfupdate")}</label>
  </div>

  <div class="grid adv-only">
    <div>
      <label>{t("web_healthcheck_max_starting")} {help_(t("web_healthcheck_max_starting_hint"))}{env_("healthcheck_max_starting")}</label>
      <input type="number" name="healthcheck_max_starting" value="{_e(config.healthcheck_max_starting)}" min="30" max="3600" form="settings-form">
    </div>
    <div>
      <label>{t("web_docker_stop_timeout")} {help_(t("web_docker_stop_timeout_hint"))}{env_("docker_stop_timeout")}</label>
      <input type="number" name="docker_stop_timeout" value="{_e(config.docker_stop_timeout)}" min="1" max="3600" form="settings-form">
    </div>
  </div>
  <p class="form-help">{t("web_updates_tab_hint")}</p>
<!-- In Updates: an update window is an update setting. -->
<div class="card adv-only" id="windows">
<h2>{t("web_windows_title")}</h2>
<p class="card-intro">{t("web_windows_intro")}</p>
{self._windows_html(t)}
</div>
</div>

<!-- ── Aufräumen ─────────────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="cleanup">
  <div class="form-checkbox-row">
    <input type="checkbox" name="auto_cleanup" id="cb-auto-cl" {cb(config.auto_cleanup)} form="settings-form">
    <label for="cb-auto-cl">{t("web_auto_cleanup")}{env_("auto_cleanup")}</label>
  </div>
  <p class="form-help">{t("web_auto_cleanup_hint")}</p>

  <div class="grid adv-only">
    <div>
      <label>{t("web_cleanup_grace_hours")} {help_(t("web_cleanup_grace_hours_hint"))}{env_("cleanup_grace_hours")}</label>
      <input type="number" name="cleanup_grace_hours" value="{_e(config.cleanup_grace_hours)}" min="0" max="8760" form="settings-form">
    </div>
    <div>
      <label>{t("web_cleanup_backup_days")} {help_(t("web_cleanup_backup_days_hint"))}{env_("cleanup_backup_days")}</label>
      <input type="number" name="cleanup_backup_days" value="{_e(config.cleanup_backup_days)}" min="1" max="365" form="settings-form">
    </div>
  </div>
  <div class="form-checkbox-row adv-only">
    <input type="checkbox" name="cleanup_backup_local_only" id="cb-bak-local" {cb(config.cleanup_backup_local_only)} form="settings-form">
    <label for="cb-bak-local">{t("web_cleanup_backup_local_only")}{env_("cleanup_backup_local_only")}</label>
  </div>
  <p class="form-help adv-only">{t("web_cleanup_backup_local_only_hint")}</p>
<!-- In Cleanup: maintenance mode pauses the actions below. -->
<div class="card">
<h2>{t("web_maint_mode_title")}</h2>
<p class="card-intro">{t("web_maint_mode_intro")}</p>
{self._maint_mode_html(t)}
</div>
<!-- In Cleanup, beside the cleanup it runs. -->
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
</div>

<!-- ── Benachrichtigungen ────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="notifs">
  <div class="grid adv-only">
    <div>
      <label>{t("web_disk_warn_percent")} {help_(t("web_disk_warn_percent_hint"))}{env_("disk_warn_percent")}</label>
      <input type="number" name="disk_warn_percent" value="{_e(config.disk_warn_percent)}" min="50" max="100" form="settings-form">
    </div>
    <div>
      <div class="form-checkbox-row" style="margin-top:24px">
        <input type="checkbox" name="disk_warn_auto_cleanup" id="cb-disk-acl" {cb(config.disk_warn_auto_cleanup)} form="settings-form">
        <label for="cb-disk-acl">{t("web_disk_warn_auto_cleanup")}{env_("disk_warn_auto_cleanup")}</label>
      </div>
      <p class="form-help">{t("web_disk_warn_auto_cleanup_hint")}</p>
    </div>
  </div>

  <hr class="section-divider adv-only">

  <div class="grid">
    <div>
      <label>{t("web_quiet_hours_start")}{env_("quiet_hours_start")}</label>
      <input type="text" name="quiet_hours_start" value="{_e(config.quiet_hours_start)}" placeholder="22:00" pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$|^$" form="settings-form">
    </div>
    <div>
      <label>{t("web_quiet_hours_end")}{env_("quiet_hours_end")}</label>
      <input type="text" name="quiet_hours_end" value="{_e(config.quiet_hours_end)}" placeholder="07:00" pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$|^$" form="settings-form">
    </div>
  </div>
  <p class="form-help">{t("web_quiet_hours_hint")}</p>

  <hr class="section-divider adv-only">

  <div class="adv-only">
  <h3 style="font-size:14px;color:var(--accent);margin-bottom:8px">{t("web_weekly_title")}</h3>
  <div class="form-checkbox-row">
    <input type="checkbox" name="weekly_report_enabled" id="cb-weekly" {cb(config.weekly_report_enabled)} form="settings-form">
    <label for="cb-weekly">{t("web_weekly_enable")}{env_("weekly_report_enabled")}</label>
  </div>
  <p class="form-help">{t("web_weekly_hint")}</p>
  <div class="grid">
    <div>
      <label>{t("web_weekly_day")}{env_("weekly_report_weekday")}</label>
      <select name="weekly_report_weekday" form="settings-form">
        {''.join(f'<option value="{i}" {"selected" if int(config.weekly_report_weekday or 0)==i else ""}>{name}</option>' for i, name in enumerate([t("web_weekday_mon"), t("web_weekday_tue"), t("web_weekday_wed"), t("web_weekday_thu"), t("web_weekday_fri"), t("web_weekday_sat"), t("web_weekday_sun")]))}
      </select>
    </div>
    <div>
      <label>{t("web_weekly_hour")}{env_("weekly_report_hour")}</label>
      <input type="number" name="weekly_report_hour" value="{_e(config.weekly_report_hour)}" min="0" max="23" form="settings-form">
    </div>
  </div>
  </div>

  <hr class="section-divider">

  <h3 style="font-size:14px;color:var(--accent);margin-bottom:8px">{t("web_monitor_title")}</h3>
  <div class="form-checkbox-row">
    <input type="checkbox" name="monitor_enabled" id="cb-monitor" {cb(config.monitor_enabled)} form="settings-form">
    <label for="cb-monitor">{t("web_monitor_enabled")} {help_(t("web_monitor_hint"))}{env_("monitor_enabled")}</label>
  </div>
  <div class="grid">
    <div>
      <label>{t("web_monitor_interval")} {help_(t("web_monitor_interval_hint"))}{env_("monitor_interval_seconds")}</label>
      <input type="number" name="monitor_interval_seconds" value="{_e(config.monitor_interval_seconds)}" min="15" max="86400" form="settings-form">
    </div>
  </div>
</div>

<div style="margin-top:16px">
  <button type="submit" class="btn" form="settings-form">{t("web_save")}</button>
</div>

</div>

"""

            content = self._settings_notices(t, content) + content
            self._send_html(self._render_page(content, "settings"))

        def _settings_notices(self, t, page_html):
            """Two cards above the settings, each answering "why is this
            not what I set?".

            Both come out of @famewolf's night in #2. He spent four hours
            hunting a setting that was switched on and hidden by simple
            mode, and separately found three of his environment variables
            being overruled by saved values with no way to undo it short
            of editing settings.json by hand.
            """
            cards = ""

            # ── settings that are on, but not on screen ─────────────────
            # Only in simple mode: in advanced mode nothing is hidden, so
            # there is nothing to disclose.
            if getattr(config, "ui_mode", "advanced") == "simple":
                try:
                    import settings_notices
                    from config import PERSISTENT_ENV_DEFAULTS, LOGGABLE_PERSISTENT_KEYS
                    labels = {}
                    active = [
                        (k, v, lbl) for k, v, lbl in settings_notices.active_hidden(
                            config, page_html, PERSISTENT_ENV_DEFAULTS, labels)
                        if k in LOGGABLE_PERSISTENT_KEYS
                    ]
                except Exception:
                    active = []
                if active:
                    rows = "".join(
                        f"<li><code>{_e(k)}</code> — "
                        f"{_e(settings_notices.as_text(v))}</li>"
                        for k, v, _lbl in active)
                    cards += f"""
<div class="card">
<h2>{t("web_hidden_active_title")}</h2>
<p class="card-intro">{t("web_hidden_active_intro", count=len(active))}</p>
<ul class="notice-list">{rows}</ul>
<form method="POST" action="/api/ui_mode">
  <input type="hidden" name="mode" value="advanced">
  <button type="submit" class="btn">{t("web_hidden_active_show")}</button>
</form>
</div>
"""

            # ── environment variables that a saved value is overruling ──
            overrides = list(getattr(config, "env_overrides", []) or [])
            if overrides:
                rows = ""
                for o in overrides:
                    env_v = o["var"] if o["secret"] else f"{o['var']}={o['env']}"
                    saved = ("(hidden)" if o["secret"]
                             else (o["saved"] or "(empty)"))
                    rows += f"""
<tr>
  <td><code>{_e(env_v)}</code></td>
  <td>{_e(saved)}</td>
  <td>
    <form method="POST" action="/api/env_adopt" class="inline-form">
      <input type="hidden" name="key" value="{_e(o['key'])}">
      <button type="submit" class="btn btn-sm">{t("web_env_adopt")}</button>
    </form>
  </td>
</tr>"""
                cards += f"""
<div class="card">
<h2>{t("web_env_card_title")}</h2>
<p class="card-intro">{t("web_env_card_intro")}</p>
<table>
<thead><tr><th>{t("web_env_card_env")}</th><th>{t("web_env_card_saved")}</th><th></th></tr></thead>
<tbody>{rows}</tbody>
</table>
</div>
"""
            return cards

        def _page_connections(self):
            """Where the notification channels are configured.

            These lived on the Settings page's Channels tab until the
            interactive Discord bot arrived (#57) and made it the longest
            tab on the page — with SMTP, ntfy, Matrix, Apprise and Gotify
            still to come, all of which are environment-only today, it
            would have stopped being a form and started being a wall.

            Its own page also matches how people think about it: the rest
            of Settings is about *when* Docksentry acts, this is about
            *where it talks*. Each channel is a card that can be read on
            its own, and a card is where a "send a test" button belongs.
            """
            t = _web_translator(config.language)
            help_ = self._help
            env_ = lambda key: self._env_mark(key, t)

            # ── What happened to the Discord bot on the last save ────
            # The bot starts on a background thread and its failures are
            # console output, which is exactly where nobody is looking
            # when they have just typed a token into a web form. The POST
            # handler puts a closed set of codes in the query string and
            # this turns them into a sentence. `discord_detail` is
            # whatever Discord's API actually said — untranslated on
            # purpose, because it is a quote, and escaped like any other
            # value that arrived over the wire.
            #
            # getattr, because the render tests build a handler with
            # __new__ and never give it a path — a page that cannot be
            # rendered without a live request is a page that cannot be
            # tested.
            _dq = parse_qs(urlparse(getattr(self, "path", "") or "").query)
            _dcode = (_dq.get("discord", [""])[0] or "").strip()
            _ddetail = (_dq.get("discord_detail", [""])[0] or "").strip()
            _dstyles = {
                "ok": ("var(--success)", t("web_discord_bot_started")),
                "disabled": ("var(--text-muted)", t("web_discord_bot_off")),
                "guild": ("var(--warn)", t("web_discord_bot_no_guild")),
                "token": ("var(--danger)", t("web_discord_bot_bad_token")),
                "register": ("var(--warn)", t("web_discord_bot_no_commands")),
                "restart_needed": ("var(--text-muted)",
                                   t("web_discord_bot_restart_needed")),
                "error": ("var(--danger)", t("web_discord_bot_error")),
            }
            discord_bot_notice = ""
            if _dcode in _dstyles:
                _col, _msg = _dstyles[_dcode]
                _extra = (f' <span style="opacity:.75">({_e(_ddetail)})</span>'
                          if _ddetail else "")
                discord_bot_notice = (
                    f'<p style="font-size:13px;color:{_col};margin:0 0 8px">'
                    f'{_e(_msg)}{_extra}</p>')

            # Clearing the token needs its own act. The field renders
            # blank on every load — a saved secret is never written back
            # into HTML — so "empty means unchanged" is the only safe
            # reading of an empty field, and that leaves no way to say
            # "remove it". Hence the checkbox, and only when there is
            # something to remove.
            # Same reasoning as the bot token below: a saved SMTP password
            # is never written back into the page, so an empty field can
            # only mean "leave it alone", and removing it needs its own act.
            smtp_clear_row = ""
            if config.smtp_password:
                smtp_clear_row = (
                    '<div class="form-checkbox-row">'
                    '<input type="checkbox" name="smtp_password_clear" '
                    'id="cb-smtp-clear" form="conn-form">'
                    f'<label for="cb-smtp-clear">{t("web_smtp_password_clear")}'
                    f' {help_(t("web_smtp_password_clear_help"))}</label></div>')

            _tls = (config.smtp_tls or "starttls").lower()
            smtp_tls_options = "".join(
                f'<option value="{v}"{" selected" if v == _tls else ""}>'
                f'{_e(t(k))}</option>'
                for v, k in (("starttls", "web_smtp_tls_starttls"),
                             ("ssl", "web_smtp_tls_ssl"),
                             ("none", "web_smtp_tls_none")))

            # ── the state of each channel, and its switch ────────────
            # Three states, deliberately, because they need three
            # different things done about them: sending, switched off,
            # and incomplete. One boolean answers none of the questions
            # someone asks when nothing is arriving.
            from notifier import Notifier as _Notifier
            _states = {n: (en, ok, miss)
                       for n, en, ok, miss in _Notifier(config).channel_states()}

            def channel_head(name, label=""):
                """State line, on/off switch and test button for a card.

                `label` is only passed where a card holds more than one
                channel — the Webhooks card has two, and two unlabelled
                state lines above two fields leave you counting rows to
                work out which is which.
                """
                enabled, complete, missing = _states.get(name, (True, False, []))
                lead = f'<b>{_e(label)}</b> ' if label else ""
                if not complete:
                    what = ", ".join(t(k) for k in missing) or t("web_chan_unset")
                    state = (f'<span style="color:var(--text-muted)">'
                             f'{lead}{_e(t("web_chan_incomplete", missing=what))}</span>')
                elif not enabled:
                    state = (f'<span style="color:var(--warn)">'
                             f'{lead}{_e(t("web_chan_off"))}</span>')
                else:
                    state = (f'<span style="color:var(--success)">'
                             f'{lead}{_e(t("web_chan_active"))}</span>')
                cid = f"cb-chan-{name}"
                # The switch is only offered once the channel could
                # actually work. Turning something on that then does
                # nothing is the failure this whole line exists to stop,
                # so the control is absent rather than present-and-inert
                # — a disabled checkbox invites clicking it and explains
                # nothing.
                switch = ""
                if complete:
                    switch = (
                        f'<input type="hidden" '
                        f'name="channel_{name}_enabled_shown" value="1" '
                        f'form="conn-form">'
                        f'<label class="form-checkbox-row" style="margin:0">'
                        f'<input type="checkbox" name="channel_{name}_enabled" '
                        f'id="{cid}" {"checked" if enabled else ""} '
                        f'form="conn-form">'
                        f'<span>{t("web_chan_enable")}</span></label>')
                test = (f'<button type="button" class="btn-sm btn-outline" '
                        f'onclick="dsTestChannel(\'{name}\')">'
                        f'{t("web_test_send")}</button>' if complete else "")
                # What this way can DO, next to what it is doing. Every
                # notifier channel is one-way: it sends and cannot be
                # spoken to. Telegram and the Discord bot can, and they
                # say so on their own cards. Without it, "Discord" on two
                # different cards means two different things and nothing
                # on the page says which.
                can = (f'<span class="badge" style="font-weight:400">'
                       f'{_e(t("web_chan_send_only"))}</span>')
                return (f'<div style="display:flex;align-items:center;gap:14px;'
                        f'flex-wrap:wrap;margin:0 0 10px">{state}{can}{switch}{test}'
                        f'</div>')

            def secret_field(name, label, help_text, current, placeholder):
                """A masked credential: field, plus a way to remove it.

                Five channels grew one of these at once, and five
                hand-written copies is five chances to render the stored
                value back into `value=` by accident. The rules are the
                same for all of them and live here: the field is always
                blank, an empty submission means "unchanged", and the
                clear checkbox appears only when there is something to
                clear.
                """
                cid = "cb-clear-" + name.replace("_", "-")
                ph = t("web_secret_saved") if current else placeholder
                html_ = (
                    f'<label>{label} {help_(help_text)}{env_(name)}</label>'
                    f'<input type="password" name="{name}" value="" '
                    f'autocomplete="new-password" placeholder="{_e(ph)}" '
                    f'form="conn-form">')
                if current:
                    html_ += (
                        f'<div class="form-checkbox-row">'
                        f'<input type="checkbox" name="{name}_clear" '
                        f'id="{cid}" form="conn-form">'
                        f'<label for="{cid}">{t("web_secret_clear")}</label>'
                        f'</div>')
                return html_

            discord_clear_row = ""
            if config.discord_bot_token:
                discord_clear_row = (
                    '<div class="form-checkbox-row">'
                    '<input type="checkbox" name="discord_bot_token_clear" '
                    'id="cb-discord-clear" form="conn-form">'
                    f'<label for="cb-discord-clear">{t("web_discord_token_clear")}'
                    f' {help_(t("web_discord_token_clear_help"))}</label></div>')

            # Both Discord paths configured means every notification
            # arrives twice — the webhook posts it and the bot posts it.
            # @NotRetarded spotted this before it existed (#57) and asked
            # for a restriction that forbids both. Said, not forbidden:
            # somebody may genuinely want the webhook in a public channel
            # and the bot in a private one, and a hard block takes that
            # away. Same line the rest of this interface takes — tell
            # them, let them decide.
            _dup = ""
            if (config.discord_webhook
                    and config.discord_bot_token
                    and config.discord_bot_channel
                    and getattr(config, "channel_discord_enabled", True)
                    and getattr(config, "channel_discordbot_enabled", True)):
                _dup = (f'<div class="card card-warn">'
                        f'<h2>{_ICONS["alert"]} {t("web_discord_dup_title")}</h2>'
                        f'<p class="card-intro">{t("web_discord_dup_intro")}</p>'
                        f'</div>')

            # No inline "saved" line: app.js already turns `?saved=1` and
            # `?error=` into a toast for every page, and two success
            # messages for one save reads like something happened twice.
            # The Discord notice above is different — it survives on the
            # page because it can carry a failure you need to act on.

            # Same construction as the Settings page: one empty form, and
            # every control associates with it by id. Nesting a second
            # form inside a card would silently close this one and drop
            # every field after it — see scripts/test_form_nesting.py.
            # ── the cards, ordered by what they are doing ───────────
            # Active first, then switched off, then the ones that are not
            # set up. Eight cards is a lot to scroll, and the whole point
            # of the state lines is answering "which channels are live?"
            # at a glance — an order that buries the two that work under
            # five that are not configured defeats it.
            #
            # NOT tabs, for the same reason: a tab shows one channel at a
            # time, which is exactly the question the state lines exist to
            # answer without clicking seven times.
            _cards = {
                # One card for everything Discord. Three of them,
                # scattered through a list sorted by state, is what
                # @NotRetarded actually saw (#57): "they're all over
                # the place". They remain three separate things — a
                # webhook, the command bot, and the bot's own channel
                # — so each keeps its own state line and switch. The
                # card groups them; nothing underneath changed.
                "discord": f"""<div class="card">
<h2>Discord</h2>

<h3 style="font-size:14px;color:var(--accent);margin:6px 0 4px">{t("web_conn_discord_hook")}</h3>
<p class="card-intro" style="margin-top:0">{t("web_conn_discord_hook_intro")}</p>
{channel_head("discord")}
  <label>Discord Webhook {help_(t("web_discord_help"))}{env_("discord_webhook")}</label>
  <input type="text" name="discord_webhook" id="f-discord_webhook" value="{_e(config.discord_webhook)}" placeholder="https://discord.com/api/webhooks/..." style="flex:1" form="conn-form">

<h3 style="font-size:14px;color:var(--accent);margin:22px 0 4px">{t("web_discord_bot_title")}</h3>
<p class="card-intro" style="margin-top:0">{t("web_discord_bot_intro")}</p>
{discord_bot_notice}

  <label>{t("web_discord_token")} {help_(t("web_discord_token_help"))}{env_("discord_bot_token")}</label>
  <input type="password" name="discord_bot_token" value="" autocomplete="new-password"
         placeholder="{_e(t('web_discord_token_set') if config.discord_bot_token else t('web_discord_token_placeholder'))}" form="conn-form">
  {discord_clear_row}

  <div class="grid">
    <div>
      <label>{t("web_discord_app_id")} {help_(t("web_discord_app_id_help"))}{env_("discord_app_id")}</label>
      <input type="text" name="discord_app_id" value="{_e(config.discord_app_id)}" inputmode="numeric" placeholder="{_e(t('web_discord_id_placeholder'))}" form="conn-form">
    </div>
    <div>
      <label>{t("web_discord_guild_id")} {help_(t("web_discord_guild_id_help"))}{env_("discord_guild_id")}</label>
      <input type="text" name="discord_guild_id" value="{_e(config.discord_guild_id)}" inputmode="numeric" placeholder="{_e(t('web_discord_id_placeholder'))}" form="conn-form">
    </div>
  </div>

  <div class="form-checkbox-row">
    <input type="checkbox" name="discord_public_replies" id="cb-discord-public" {'checked' if config.discord_public_replies else ''} form="conn-form">
    <label for="cb-discord-public">{t("web_discord_public_replies")} {help_(t("web_discord_public_replies_help"))}{env_("discord_public_replies")}</label>
  </div>

  <div class="adv-only">
    <label>{t("web_discord_allowed_users")} {help_(t("web_discord_allowed_users_help"))}{env_("discord_allowed_users")}</label>
    <input type="text" name="discord_allowed_users" value="{_e(', '.join(str(u) for u in (config.discord_allowed_users or [])))}" placeholder="{_e(t('web_allowed_users_placeholder'))}" form="conn-form">
  </div>

<h3 style="font-size:14px;color:var(--accent);margin:22px 0 4px">{t("web_conn_discordbot")}</h3>
<p class="card-intro" style="margin-top:0">{t("web_conn_discordbot_intro")}</p>
{channel_head("discordbot")}
  <label>{t("web_discord_bot_channel")} {help_(t("web_discord_bot_channel_help"))}{env_("discord_bot_channel")}</label>
  <input type="text" name="discord_bot_channel" value="{_e(config.discord_bot_channel)}" inputmode="numeric" placeholder="{_e(t('web_discord_id_placeholder'))}" form="conn-form">
</div>
""",
                "webhook": f"""<div class="card">
<h2>Webhook</h2>
<p class="card-intro">{t("web_conn_webhook_intro")}</p>
{channel_head("webhook")}
  <label>Webhook URL {help_(t("web_webhook_help"))}{env_("webhook_url")}</label>
  <input type="text" name="webhook_url" id="f-webhook_url" value="{_e(config.webhook_url)}" placeholder="https://your-service/webhook" style="flex:1" form="conn-form">
</div>
""",
                "smtp": f"""<div class="card">
<h2>{t("web_conn_smtp")}</h2>
<p class="card-intro">{t("web_conn_smtp_intro")}</p>
{channel_head("smtp")}
  <div class="grid">
    <div>
      <label>{t("web_smtp_host")} {help_(t("web_smtp_host_help"))}{env_("smtp_host")}</label>
      <input type="text" name="smtp_host" value="{_e(config.smtp_host)}" placeholder="{_e(t('web_smtp_host_placeholder'))}" form="conn-form">
    </div>
    <div>
      <label>{t("web_smtp_port")} {help_(t("web_smtp_port_help"))}{env_("smtp_port")}</label>
      <input type="number" name="smtp_port" value="{_e(config.smtp_port)}" min="1" max="65535" form="conn-form">
    </div>
  </div>

  <label>{t("web_smtp_tls")} {help_(t("web_smtp_tls_help"))}{env_("smtp_tls")}</label>
  <select name="smtp_tls" form="conn-form">{smtp_tls_options}</select>

  <div class="grid">
    <div>
      <label>{t("web_smtp_from")} {help_(t("web_smtp_from_help"))}{env_("smtp_from")}</label>
      <input type="text" name="smtp_from" value="{_e(config.smtp_from)}" placeholder="docksentry@example.com" form="conn-form">
    </div>
    <div>
      <label>{t("web_smtp_to")} {help_(t("web_smtp_to_help"))}{env_("smtp_to")}</label>
      <input type="text" name="smtp_to" value="{_e(config.smtp_to)}" placeholder="{_e(t('web_smtp_to_placeholder'))}" form="conn-form">
    </div>
  </div>

  <div class="grid">
    <div>
      <label>{t("web_smtp_user")} {help_(t("web_smtp_user_help"))}{env_("smtp_user")}</label>
      <input type="text" name="smtp_user" value="{_e(config.smtp_user)}" autocomplete="off" form="conn-form">
    </div>
    <div>
      <label>{t("web_smtp_password")} {help_(t("web_smtp_password_help"))}{env_("smtp_password")}</label>
      <input type="password" name="smtp_password" value="" autocomplete="new-password"
             placeholder="{_e(t('web_smtp_password_set') if config.smtp_password else t('web_smtp_password_placeholder'))}" form="conn-form">
    </div>
  </div>
  {smtp_clear_row}

  <div class="form-checkbox-row adv-only">
    <input type="checkbox" name="smtp_tls_verify" id="cb-smtp-verify" {'checked' if config.smtp_tls_verify else ''} form="conn-form">
    <label for="cb-smtp-verify">{t("web_smtp_tls_verify")} {help_(t("web_smtp_tls_verify_help"))}{env_("smtp_tls_verify")}</label>
  </div>
</div>
""",
                "ntfy": f"""<div class="card">
<h2>{t("web_conn_ntfy")}</h2>
<p class="card-intro">{t("web_conn_ntfy_intro")}</p>
{channel_head("ntfy")}
  <label>{t("web_ntfy_url")} {help_(t("web_ntfy_url_help"))}{env_("ntfy_url")}</label>
  <input type="text" name="ntfy_url" value="{_e(config.ntfy_url)}" placeholder="https://ntfy.sh/my-topic" form="conn-form">
  <div class="grid">
    <div>
      <label>{t("web_ntfy_server")} {help_(t("web_ntfy_server_help"))}{env_("ntfy_server")}</label>
      <input type="text" name="ntfy_server" value="{_e(config.ntfy_server)}" placeholder="https://ntfy.sh" form="conn-form">
    </div>
    <div>
      <label>{t("web_ntfy_topic")} {help_(t("web_ntfy_topic_help"))}{env_("ntfy_topic")}</label>
      <input type="text" name="ntfy_topic" value="{_e(config.ntfy_topic)}" placeholder="my-topic" form="conn-form">
    </div>
  </div>
  {secret_field("ntfy_token", t("web_ntfy_token"), t("web_ntfy_token_help"), config.ntfy_token, t("web_ntfy_token_placeholder"))}
  <div class="adv-only">
    <label>{t("web_ntfy_user")} {help_(t("web_ntfy_user_help"))}{env_("ntfy_user")}</label>
    <input type="text" name="ntfy_user" value="{_e(config.ntfy_user)}" autocomplete="off" form="conn-form">
    {secret_field("ntfy_password", t("web_ntfy_password"), t("web_ntfy_password_help"), config.ntfy_password, t("web_ntfy_password_placeholder"))}
  </div>
</div>
""",
                "gotify": f"""<div class="card">
<h2>{t("web_conn_gotify")}</h2>
<p class="card-intro">{t("web_conn_gotify_intro")}</p>
{channel_head("gotify")}
  <label>{t("web_gotify_url")} {help_(t("web_gotify_url_help"))}{env_("gotify_url")}</label>
  <input type="text" name="gotify_url" value="{_e(config.gotify_url)}" placeholder="https://gotify.example.com" form="conn-form">
  {secret_field("gotify_token", t("web_gotify_token"), t("web_gotify_token_help"), config.gotify_token, t("web_gotify_token_placeholder"))}
</div>
""",
                "matrix": f"""<div class="card">
<h2>{t("web_conn_matrix")}</h2>
<p class="card-intro">{t("web_conn_matrix_intro")}</p>
{channel_head("matrix")}
  <div class="grid">
    <div>
      <label>{t("web_matrix_homeserver")} {help_(t("web_matrix_homeserver_help"))}{env_("matrix_homeserver")}</label>
      <input type="text" name="matrix_homeserver" value="{_e(config.matrix_homeserver)}" placeholder="https://matrix.org" form="conn-form">
    </div>
    <div>
      <label>{t("web_matrix_room")} {help_(t("web_matrix_room_help"))}{env_("matrix_room")}</label>
      <input type="text" name="matrix_room" value="{_e(config.matrix_room)}" placeholder="#docksentry:matrix.org" form="conn-form">
    </div>
  </div>
  {secret_field("matrix_token", t("web_matrix_token"), t("web_matrix_token_help"), config.matrix_token, t("web_matrix_token_placeholder"))}
</div>
""",
                "apprise": f"""<div class="card">
<h2>{t("web_conn_apprise")}</h2>
<p class="card-intro">{t("web_conn_apprise_intro")}</p>
{channel_head("apprise")}
  <label>{t("web_apprise_url")} {help_(t("web_apprise_url_help"))}{env_("apprise_url")}</label>
  <input type="text" name="apprise_url" value="{_e(config.apprise_url)}" placeholder="http://apprise:8000/notify" form="conn-form">
  {secret_field("apprise_urls", t("web_apprise_urls"), t("web_apprise_urls_help"), config.apprise_urls, t("web_apprise_urls_placeholder"))}
  <div class="adv-only">
    <label>{t("web_apprise_tag")} {help_(t("web_apprise_tag_help"))}{env_("apprise_tag")}</label>
    <input type="text" name="apprise_tag" value="{_e(config.apprise_tag)}" form="conn-form">
  </div>
</div>

""",
            }

            def _rank(name):
                enabled, complete, _ = _states.get(name, (True, False, []))
                if complete and enabled:
                    return 0
                if complete:
                    return 1
                return 2

            # Telegram is on this page and is a notification channel to
            # anyone reading it, but it is not one of the notifier
            # plugins — it is the bot, configured by BOT_TOKEN and
            # CHAT_ID. Leaving it out of the count produced "0 active" on
            # an instance whose Telegram notifications were working
            # perfectly well, which is a confidently wrong answer of
            # exactly the kind the state lines were added to stop.
            #
            # It has no switch and no fields of its own here, so it only
            # ever contributes to "active" or "not set up".
            _tg_set = bool(getattr(config, "bot_token", "")
                           and getattr(config, "chat_id", ""))
            _tg_enabled = bool(getattr(config, "channel_telegram_enabled", True))
            _tg_on = _tg_set and _tg_enabled

            if not _tg_set:
                telegram_state = (
                    f'<span style="color:var(--text-muted)">'
                    f'{_e(t("web_chan_incomplete", missing=t("web_chan_telegram_env")))}'
                    f'</span>')
            elif not _tg_enabled:
                telegram_state = (f'<span style="color:var(--warn)">'
                                  f'{_e(t("web_chan_off"))}</span>')
            else:
                telegram_state = (f'<span style="color:var(--success)">'
                                  f'{_e(t("web_chan_active"))}</span>')
            # Same switch as the seven plugin channels, and only once the
            # two variables it needs are set — the rule everywhere on
            # this page. Off means off, notifications and commands both;
            # the label says so, because "why is it still answering
            # /status?" is the next question otherwise.
            telegram_state += (f'<span class="badge" style="font-weight:400;'
                               f'margin-left:14px">'
                               f'{_e(t("web_chan_send_and_commands"))}</span>')
            if _tg_set:
                telegram_state += (
                    '<input type="hidden" name="channel_telegram_enabled_shown"'
                    ' value="1" form="conn-form">'
                    '<label class="form-checkbox-row" '
                    'style="margin:0 0 0 14px;display:inline-flex">'
                    '<input type="checkbox" name="channel_telegram_enabled" '
                    f'id="cb-chan-telegram" {"checked" if _tg_enabled else ""} '
                    'form="conn-form">'
                    f'<span>{t("web_chan_enable_telegram")}</span></label>')

            _n_active = sum(1 for n in _cards if _rank(n) == 0) + int(_tg_on)
            _n_off = sum(1 for n in _cards if _rank(n) == 1)
            _n_unset = sum(1 for n in _cards if _rank(n) == 2) + int(not _tg_on)
            _summary = (
                f'<p style="margin:8px 0 0;font-size:13px">'
                f'<b style="color:var(--success)">{_n_active}</b> '
                f'{_e(t("web_chan_sum_active"))} &middot; '
                f'<b style="color:var(--warn)">{_n_off}</b> '
                f'{_e(t("web_chan_sum_off"))} &middot; '
                f'<b style="color:var(--text-muted)">{_n_unset}</b> '
                f'{_e(t("web_chan_sum_unset"))}</p>')

            # Stable within a rank: the declaration order above, so a
            # channel does not jump around between two saves that changed
            # nothing about it.
            _ordered = "\n".join(
                _cards[n] for n in sorted(_cards, key=lambda n:
                                          (_rank(n), list(_cards).index(n))))

            content = (_dup + f"""
<form method="POST" action="/connections" id="conn-form"
      data-chan-none-title="{_e(t("web_chan_none_title"))}"
      data-chan-none-body="{_e(t("web_chan_none_body"))}"
      data-chan-none-ok="{_e(t("web_chan_none_ok"))}"></form>
<!-- Proof that a POST came from this whole form. An unchecked box
     submits nothing, so `"x" in params` reads absence as "off" —
     which is right for a page that always renders the box, and
     wrong for any other request that happens to hit this path.
     For a flag that decides whether an SMTP password is handed to
     an unverified certificate, "silently off" is not a failure
     mode worth having. -->
<input type="hidden" name="conn_page" value="1" form="conn-form">
""" + f"""<div class="card">
<h2>{t("web_connections")}</h2>
<p class="card-intro">{t("web_connections_intro")}</p>
""" + _summary
                       + "</div>\n" + f"""<div class="card">
<h2>{t("web_conn_telegram")}</h2>
<p class="card-intro">{t("web_conn_telegram_intro")}</p>
<div style="margin:0 0 10px">{telegram_state}</div>
  <div class="adv-only">
    <label>Telegram Topic ID {help_(t("web_topic_id_help"))}{env_("telegram_topic_id")}</label>
    <input type="text" name="telegram_topic_id" value="{_e(config.telegram_topic_id)}" placeholder="{_e(t('web_topic_id_placeholder'))}" form="conn-form">

    <label>{t("web_allowed_users")} {help_(t("web_allowed_users_help"))}{env_("telegram_allowed_users")}</label>
    <input type="text" name="telegram_allowed_users" value="{_e(', '.join(str(u) for u in (config.telegram_allowed_users or [])))}" placeholder="{_e(t('web_allowed_users_placeholder'))}" form="conn-form">

  </div>
</div>

<!-- BOT_LABEL is not a Telegram setting. Seven of the nine channels
     prefix their messages with it, and it sat between "Telegram Topic
     ID" and "Allowed users" — where @NotRetarded, who uses Discord,
     reasonably read it as a Telegram thing and never touched it (#2).
     Its own card, above the per-channel ones, because it applies to all
     of them. -->
<div class="card">
<h2>{t("web_label_card_title")}</h2>
<p class="card-intro">{t("web_label_card_intro")}</p>
<label>{t("web_bot_label")} {help_(t("web_bot_label_help"))}{env_("bot_label")}</label>
<input type="text" name="bot_label" value="{_e(config.bot_label or '')}" placeholder="{_e(t('web_bot_label_placeholder'))}" form="conn-form">
</div>
"""
                       + _ordered
                       + self._api_token_card(t)
                       + f"""<!-- One Save for the whole page, outside the last card. Inside it, the
     button reads as "save Discord" — and it does not, it saves every
     card above as well. -->
<div style="margin-top:16px">
  <button type="submit" class="btn" form="conn-form">{t("web_save")}</button>
</div>

""")

            self._send_html(self._render_page(content, "connections"))

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
            # Not hardcoded English: this list is rendered next to the
            # update-window controls in whatever language the UI is set to,
            # and "Mon Tue Wed" in a German page is the same locale leak the
            # cron preview had.
            _wd = t("web_weekdays_short").split(",")
            wd_full = ([w.strip() for w in _wd] if len(_wd) == 7
                       else ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
            wd_html = ""
            for i, label in enumerate(wd_full):
                wd_html += (f'<label style="display:inline-block;margin-right:10px;font-size:13px">'
                            f'<input type="checkbox" name="weekdays" value="{i}" '
                            f'style="width:auto;margin-right:4px">{label}</label>')

            return f"""<div class="table-scroll"><table style="margin-bottom:14px">
<tr><th>{t("web_name")}</th><th>{t("web_windows_range")}</th><th>{t("web_windows_days")}</th><th>{t("web_actions")}</th></tr>
{rows_html}
</table></div>
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
            # Open on Docksentry rather than on nothing. The page used to
            # render an empty frame until you picked something, and the
            # container people want first is almost always this one — it is
            # what tells you why an update failed (#2, @NotRetarded).
            if not container:
                container = self._own_container_name_safe()
            # 100, not 50. Fifteen containers already produce more than 50
            # lines of Docksentry's own output, so the old default cut off
            # the part worth reading.
            try:
                lines = max(10, min(int(query.get("lines", ["100"])[0]), 500))
            except (TypeError, ValueError):
                lines = 100

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
                # Every managed host, not just the local one. This read
                # `checker.check_all()` — one checker, no loop — so on a
                # multi-host install the button checked the machine
                # Docksentry runs on and quietly ignored the rest.
                # Measured on a two-host demo: four containers with moving
                # tags, two of them on `nas`, and the button reported
                # "Checking 2 containers for updates" without a word about
                # the other host. The scheduler had always looped; only
                # the manual paths had not.
                #
                # No `bot=` here: with DEBUG on, check_all pushes its whole
                # debug log to Telegram for the requester — right for the
                # Telegram /check command, spam for a Web UI click (the log
                # is on the /logs page anyway). Found-updates notifications
                # below are unaffected. (#35 feedback, @NotRetarded)
                from hosts import host_checkers
                found = []
                for host_checker, host_name in host_checkers(hosts, checker):
                    try:
                        found.extend(host_checker.check_all())
                    except Exception as e:
                        # One unreachable host must not cost the others
                        # their check — the same rule the scheduler
                        # follows, and the reason it reports per host.
                        where = f" on {host_name}" if host_name else ""
                        print(f"Web UI check error{where}: {e}")
                if found:
                    bot.notify_updates(found)
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
                    # No icon added here: `cleanup_images` returns a
                    # message that already starts with ✅ or ❌, and this
                    # used to put a second one in front of it.
                    bot.send_message(msg)
                if bot.notifier and bot.notifier.has_channels():
                    bot.notifier.send_message(
                        bot.t("cleanup_manual_prefix", message=msg))
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


#: Errors that mean "the client went away", not "Docksentry is broken".
#: A browser produces these routinely — it abandons a request when you
#: navigate away, cancels a speculative connection it decided not to use,
#: or closes a tab while a response is still being written.
_CLIENT_GONE = (ConnectionResetError, ConnectionAbortedError,
                BrokenPipeError, TimeoutError)


class _QuietHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that does not shout about a client hanging up.

    socketserver's `handle_error` prints a full traceback to stderr for
    any exception out of a request thread, headed "Exception occurred
    during processing of request from …". For a client that reset the
    connection that is thirteen lines of Python internals describing
    something entirely normal, and it lands in `docker logs` next to
    everything that is actually wrong.

    @NotRetarded filed it as a bug (#58) — reasonably, because it looks
    exactly like one:

        ConnectionResetError: [Errno 104] Connection reset by peer
          File ".../http/server.py", line 408, in handle_one_request
            self.raw_requestline = self.rfile.readline(65537)

    Nothing there says "your browser closed a socket". So the traceback
    was the defect, not the reset: a log line that sends someone to open
    an issue about a healthy system has cost them time and told them
    nothing. Client-gone errors now produce one line, and only in debug;
    everything else keeps the full traceback, because anything else out
    of a request thread IS a bug and hiding it would be worse than the
    noise.
    """

    #: Set by WebUI.start() so the quiet line can be gated on DEBUG.
    debug = False

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, _CLIENT_GONE):
            if self.debug:
                print(f"Web UI: client {client_address[0]} closed the "
                      f"connection ({type(exc).__name__}) — harmless")
            return
        super().handle_error(request, client_address)


class WebUI:
    def __init__(self, config, checker, bot, store, port=8080, password="",
                 backend=None, hosts=None, restart_discord=None):
        self.config = config
        self.port = port
        self.handler = create_handler(config, checker, bot, store,
                                      password or None, backend, hosts,
                                      restart_discord=restart_discord)
        self.server = None
        self.thread = None

    def start(self):
        self.server = _QuietHTTPServer(("0.0.0.0", self.port), self.handler)
        self.server.debug = bool(getattr(self.config, "debug", False))
        # Shared by every request thread; AuditLog serialises its own
        # writes, so one instance is what we want rather than one per
        # request racing on the same file.
        from audit import AuditLog
        self.server.audit = AuditLog(self.config)
        # `{token name: last used, epoch}`. Deliberately in memory and not
        # on disk: a Prometheus scraper hits `/metrics` every few seconds,
        # and a file written that often to record "still being scraped"
        # would be a lot of I/O to learn nothing. The cost is that a
        # restart clears it, so the page says "since start" rather than
        # implying a token has never been used.
        self.server.token_seen = {}
        # Browser sessions (#60). In memory on purpose, and the interface
        # says so: writing them down would put live credentials on disk,
        # which is most of what this change is trying to get away from.
        # A restart signs everyone out.
        from webauth import SessionStore
        self.server.sessions = SessionStore(
            idle_seconds=int(getattr(self.config, "web_session_hours", 8)) * 3600,
            max_seconds=int(getattr(self.config, "web_session_max_days", 7)) * 86400)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        print(f"Web UI started on port {self.port}")

    def stop(self):
        if self.server:
            self.server.shutdown()
