#!/usr/bin/env python3
"""Test the shared link validator (#52, @LeeNX).

`container_store.is_safe_link` gates every user-controlled URL that ends
up rendered — Markdown link in Telegram/Discord, `<a href>` in the Web UI.
There is no CSP header and `html.escape()` does not touch URL schemes, so
this function is the whole defence against `javascript:` in an href.

Pure logic — no Docker, no filesystem.
"""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from container_store import is_safe_link, MAX_LINK_LENGTH


def main():
    checks = {}

    # ── Script-bearing schemes ─────────────────────────────────
    # urlparse-based, so case and whitespace tricks that beat a
    # startswith("http") check are caught too.
    checks["reject: javascript:"] = is_safe_link("javascript:alert1") is False
    checks["reject: JavaScript: (mixed case)"] = is_safe_link("JavaScript:alert1") is False
    checks["reject: JAVASCRIPT: (upper)"] = is_safe_link("JAVASCRIPT:alert1") is False
    checks["reject: leading space before scheme"] = is_safe_link(" javascript:alert1") is False
    checks["reject: leading tab before scheme"] = is_safe_link("\tjavascript:alert1") is False
    checks["reject: leading newline before scheme"] = is_safe_link("\njavascript:alert1") is False
    checks["reject: data:text/html"] = is_safe_link("data:text/html;base64,PHNjcmlwdD4=") is False
    checks["reject: vbscript:"] = is_safe_link("vbscript:msgbox") is False
    checks["reject: file:"] = is_safe_link("file:///etc/passwd") is False

    # ── Missing scheme / missing host ──────────────────────────
    checks["reject: scheme-relative //evil.example"] = is_safe_link("//evil.example") is False
    checks["reject: http:// without host"] = is_safe_link("http://") is False
    checks["reject: https:/// without host"] = is_safe_link("https:///changelog") is False
    checks["reject: bare hostname (no scheme)"] = is_safe_link("example.com/changelog") is False
    checks["reject: relative path"] = is_safe_link("/api/containers") is False

    # ── Length cap ─────────────────────────────────────────────
    long_url = "https://example.com/" + "a" * 600
    checks["reject: 600+ char URL"] = is_safe_link(long_url) is False
    at_cap = "https://example.com/"
    at_cap += "a" * (MAX_LINK_LENGTH - len(at_cap))
    checks["accept: exactly at length cap"] = is_safe_link(at_cap) is True
    checks["reject: one char over cap"] = is_safe_link(at_cap + "a") is False

    # ── Characters that break out of href / markdown ───────────
    checks["reject: closing paren"] = is_safe_link("https://example.com/a)b") is False
    checks["reject: opening paren"] = is_safe_link("https://example.com/a(b") is False
    checks["reject: newline inside URL"] = is_safe_link("https://example.com/a\nb") is False
    checks["reject: carriage return inside URL"] = is_safe_link("https://example.com/a\rb") is False
    checks["reject: embedded space"] = is_safe_link("https://exa mple.com") is False
    checks["reject: double quote"] = is_safe_link('https://example.com/"onclick=x') is False
    checks["reject: single quote"] = is_safe_link("https://example.com/'onclick=x") is False
    checks["reject: angle brackets"] = is_safe_link("https://example.com/<script>") is False
    checks["reject: backslash"] = is_safe_link("https://example.com\\@evil.example") is False
    checks["reject: NUL byte"] = is_safe_link("https://example.com/\x00") is False
    checks["reject: backtick"] = is_safe_link("https://example.com/`x`") is False

    # ── Non-strings / empties ──────────────────────────────────
    checks["reject: empty string"] = is_safe_link("") is False
    checks["reject: None"] = is_safe_link(None) is False
    checks["reject: non-string"] = is_safe_link(12345) is False
    checks["reject: whitespace only"] = is_safe_link("   ") is False

    # ── Legitimate links stay usable ───────────────────────────
    checks["accept: plain https URL"] = is_safe_link(
        "https://github.com/castus-pro/docksentry") is True
    checks["accept: http URL"] = is_safe_link("http://example.com/changelog") is True
    checks["accept: URL with port"] = is_safe_link(
        "https://git.example.com:8443/owner/repo/-/releases") is True
    checks["accept: query string with comma"] = is_safe_link(
        "https://example.com/releases?tags=v1,v2&sort=desc") is True
    checks["accept: fragment + encoded space"] = is_safe_link(
        "https://example.com/CHANGELOG.md%20old#v1.54.0") is True
    checks["accept: uppercase scheme (case-insensitive)"] = is_safe_link(
        "HTTPS://example.com/repo") is True
    checks["accept: IP host"] = is_safe_link("http://192.168.1.10:9000/notes") is True

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
