#!/usr/bin/env python3
"""ntfy titles with non-ASCII characters (dc#120).

Python encodes HTTP header values as latin-1. A single emoji therefore
raises UnicodeEncodeError inside urlopen, which the notifier's broad
handler swallows — the push is dropped with one log line. The README
recommends `BOT_LABEL=🖥 pve1`, so following the documentation was enough
to get no ntfy notifications at all and no explanation why.

Every expectation below was checked against a real ntfy server before
being written down.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from notifiers.ntfy import _header_value


def main():
    checks = {}

    # Pure ASCII stays untouched: the common case should remain readable
    # in logs and to any proxy in between.
    checks["ascii is left alone"] = _header_value("3 Updates") == "3 Updates"
    checks["empty is safe"] = _header_value("") == ""
    checks["None is safe"] = _header_value(None) == ""

    # Anything non-ASCII is RFC 2047 encoded. ntfy decodes it — verified
    # live: the title came back as the original text.
    emoji = _header_value("🖥 pve1")
    checks["emoji is encoded"] = emoji.startswith("=?UTF-8?B?") and emoji.endswith("?=")
    checks["the encoded value is a latin-1 header"] = bool(emoji.encode("latin-1"))

    # The important one, and the reason the condition is `isascii()` rather
    # than "does latin-1 accept it". An umlaut PASSES the latin-1 test —
    # Python encodes ü to 0xFC quite happily — but ntfy reads the header as
    # UTF-8, where a lone 0xFC is invalid. Measured against a live server:
    # `Grün Größe` sent raw arrived as `Gr<?>n Gr<?>e`. Quietly mangled
    # rather than dropped, which is the harder of the two to notice.
    umlaut = _header_value("Grün Größe")
    checks["an umlaut is encoded too"] = umlaut.startswith("=?UTF-8?B?")
    checks["an umlaut is NOT sent raw"] = umlaut != "Grün Größe"

    # Round-trip, so the encoding is verifiably reversible rather than
    # merely well-formed.
    import base64
    for original in ("🖥 pve1", "Grün Größe", "[🖥 pve1] 3 Updates"):
        enc = _header_value(original)
        payload = enc[len("=?UTF-8?B?"):-len("?=")]
        back = base64.b64decode(payload).decode("utf-8")
        checks[f"round-trips: {original[:18]}"] = back == original

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
