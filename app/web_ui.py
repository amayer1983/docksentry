#!/usr/bin/env python3
"""Optional lightweight Web UI for configuration and status."""

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import secrets
import subprocess
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse


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
_BASE_CSS = """
:root {
    /* Backgrounds */
    --bg:           #0d1117;
    --bg-elev:      #161b22;
    --bg-elev-2:    #1c2128;
    --bg-input:     #0d1117;
    /* Borders */
    --border:       #30363d;
    --border-soft:  #21262d;
    /* Text */
    --text:         #c9d1d9;
    --text-muted:   #8b949e;
    --text-faint:   #484f58;
    /* Accents */
    --accent:       #58a6ff;
    --accent-bg:    #1f2937;
    --success:      #3fb950;
    --success-bg:   #1a3a2a;
    --warn:         #d29922;
    --warn-bg:      #3a2f1a;
    --danger:       #f85149;
    --danger-bg:    #3a1a1a;
    --info:         #58a6ff;
    --info-bg:      #1a2a3a;
    --special:      #bc8cff;
    --special-bg:   #2a1a3a;
    /* Buttons */
    --btn-green:    #238636;
    --btn-green-h:  #2ea043;
    --btn-blue:     #1f6feb;
    --btn-blue-h:   #388bfd;
    /* Misc */
    --radius:       8px;
    --radius-sm:    6px;
    --radius-pill: 12px;
    --shadow:       0 1px 0 rgba(0,0,0,0.04);
    --tt-bg:        #1f2937;
    --tt-fg:        #c9d1d9;
}

/* Light theme — applied via <html data-theme="light"> */
html[data-theme="light"] {
    --bg:           #f6f8fa;
    --bg-elev:      #ffffff;
    --bg-elev-2:    #eef1f4;
    --bg-input:    #ffffff;
    --border:       #d0d7de;
    --border-soft:  #e1e4e8;
    --text:         #1f2328;
    --text-muted:   #59636e;
    --text-faint:   #818b98;
    --accent:       #0969da;
    --accent-bg:    #ddf4ff;
    --success:      #1a7f37;
    --success-bg:   #dafbe1;
    --warn:         #9a6700;
    --warn-bg:      #fff8c5;
    --danger:       #cf222e;
    --danger-bg:    #ffebe9;
    --info:         #0969da;
    --info-bg:      #ddf4ff;
    --special:      #8250df;
    --special-bg:   #fbefff;
    --btn-green:    #1f883d;
    --btn-green-h:  #1a7f37;
    --btn-blue:     #0969da;
    --btn-blue-h:   #0860c5;
    --tt-bg:        #24292f;
    --tt-fg:        #f6f8fa;
}

/* ── Reset & base ───────────────────────────────────────────── */
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
}

/* ── Header & nav ───────────────────────────────────────────── */
.header {
    background: var(--bg-elev);
    border-bottom: 1px solid var(--border);
    padding: 14px 24px;
}
.header-row {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    max-width: 900px;
    margin: 0 auto;
}
.header-brand {
    display: flex;
    align-items: center;
    gap: 10px;
    flex: 1;
    min-width: 0;
}
.header-brand img {
    height: 36px;
    width: 36px;
    flex-shrink: 0;
    border-radius: 6px;
}
.header-brand h1 {
    font-size: 18px;
    margin: 0;
    color: var(--accent);
    font-weight: 600;
    letter-spacing: -0.01em;
}
.header-host-slot { font-size: 13px; color: var(--text-muted); }
.nav-wrap { max-width: 900px; margin: 12px auto 0 auto; }
nav { display: flex; gap: 4px; flex-wrap: wrap; }
nav a {
    color: var(--text-muted);
    text-decoration: none;
    padding: 6px 14px;
    border-radius: var(--radius-sm);
    font-size: 14px;
}
nav a:hover { color: var(--text); background: var(--bg-elev-2); }
nav a.active { color: var(--accent); background: var(--accent-bg); }

.content { max-width: 900px; margin: 24px auto; padding: 0 24px; }

/* ── Cards ──────────────────────────────────────────────────── */
.card {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
    margin-bottom: 16px;
}
.card h2 { font-size: 16px; margin-bottom: 12px; color: var(--accent); }
.card-intro {
    font-size: 12px;
    color: var(--text-muted);
    margin-bottom: 12px;
}
.card-warn {
    border-color: var(--warn);
    background: linear-gradient(180deg, rgba(210,153,34,0.08) 0%, var(--bg-elev) 60%);
}
.card-warn h2 { color: var(--warn); }

/* ── Tables ─────────────────────────────────────────────────── */
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th {
    text-align: left;
    padding: 8px 12px;
    color: var(--text-muted);
    border-bottom: 1px solid var(--border);
    font-weight: 500;
}
td { padding: 10px 12px; border-bottom: 1px solid var(--border-soft); vertical-align: middle; }
tbody tr, table tr { transition: background 0.12s; }
table tr:not(:first-child):hover { background: var(--bg-elev-2); }

/* ── Forms ──────────────────────────────────────────────────── */
form { margin-top: 8px; }
label { display: block; margin-bottom: 4px; font-size: 14px; color: var(--text-muted); }
input, select, textarea {
    background: var(--bg-input);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 8px 12px;
    border-radius: var(--radius-sm);
    font-size: 14px;
    width: 100%;
    margin-bottom: 12px;
    font-family: inherit;
}
select { cursor: pointer; }
.form-help {
    font-size: 11px;
    color: var(--text-faint);
    margin: -8px 0 12px 0;
}
.form-row-inline {
    display: flex;
    gap: 8px;
    align-items: flex-start;
}
.form-row-inline > * { margin-bottom: 0; }
.form-checkbox-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 8px 0;
}
.form-checkbox-row input[type="checkbox"] {
    width: auto;
    margin: 0;
}
.form-checkbox-row label {
    margin: 0;
    color: var(--text);
    font-weight: 500;
    cursor: pointer;
}
hr.section-divider {
    border: none;
    border-top: 1px solid var(--border);
    margin: 16px 0;
}

/* ── Buttons ────────────────────────────────────────────────── */
.btn {
    background: var(--btn-green);
    color: #fff;
    border: none;
    padding: 8px 20px;
    border-radius: var(--radius-sm);
    cursor: pointer;
    font-size: 14px;
    font-family: inherit;
    transition: background 0.15s;
}
.btn:hover { background: var(--btn-green-h); }
.btn-blue { background: var(--btn-blue); }
.btn-blue:hover { background: var(--btn-blue-h); }
.btn-danger { background: var(--danger); }
.btn-danger:hover { background: #ff6b62; }
.btn-outline {
    background: transparent;
    color: var(--text-muted);
    border: 1px solid var(--border);
}
.btn-outline:hover {
    color: var(--text);
    border-color: var(--text-muted);
}
.btn-sm {
    padding: 3px 10px;
    border-radius: 4px;
    font-size: 12px;
    border: none;
    cursor: pointer;
    font-family: inherit;
}
/* Icon-only button — square-ish, just an emoji/glyph inside */
.btn-icon {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text-muted);
    width: 32px;
    height: 32px;
    border-radius: var(--radius-sm);
    cursor: pointer;
    font-size: 14px;
    line-height: 1;
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: all 0.15s;
}
.btn-icon:hover {
    color: var(--text);
    border-color: var(--text-muted);
    background: var(--bg-elev-2);
    transform: translateY(-1px);
}
.btn-icon:active { transform: translateY(0); }
.btn-icon.is-active {
    color: #fff;
    background: var(--accent);
    border-color: var(--accent);
    box-shadow: 0 0 0 2px rgba(88,166,255,0.15);
}
.btn-icon.is-active:hover {
    background: #4a8fdc;
    border-color: #4a8fdc;
    color: #fff;
}
.btn-icon.is-warn {
    color: var(--bg);
    background: var(--warn);
    border-color: var(--warn);
    box-shadow: 0 0 0 2px rgba(210,153,34,0.15);
}
.btn-icon.is-warn:hover {
    background: #b88018;
    border-color: #b88018;
    color: var(--bg);
}
.btn-icon.is-danger {
    color: #fff;
    background: var(--danger);
    border-color: var(--danger);
}
.btn-icon.is-pinned {
    color: var(--danger);
    border-color: var(--danger);
    background: rgba(248,81,73,0.08);
}
.btn-icon.is-pinned:hover { background: rgba(248,81,73,0.16); color: var(--danger); }
/* Light theme: search input icon needs darker stroke */
html[data-theme="light"] .search-input {
    background: var(--bg-input) url("data:image/svg+xml;utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%2359636e' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='11' cy='11' r='8'/%3E%3Cpath d='m21 21-4.3-4.3'/%3E%3C/svg%3E") no-repeat 10px center;
}
html[data-theme="light"] .toast {
    box-shadow: 0 4px 12px rgba(140,140,140,0.25);
}
/* SVG icons inside btn-icon use currentColor → inherit button text color */
.btn-icon svg { width: 14px; height: 14px; display: block; }
.icon-emoji { font-size: 14px; line-height: 1; }
/* Buttons with text + leading icon */
.btn-icon-text {
    display: inline-flex;
    align-items: center;
    gap: 6px;
}
.btn-icon-text svg { width: 12px; height: 12px; flex-shrink: 0; }
/* Badge with inline icon — used for "package + group name" etc. */
.badge .icon-label, .icon-label {
    display: inline-flex;
    align-items: center;
    gap: 4px;
}
.badge svg { width: 11px; height: 11px; }
/* Headings that lead with an icon */
h2 svg, h3 svg { vertical-align: middle; width: 16px; height: 16px; }

/* Container note pencil — discreet inline marker */
.note-icon {
    cursor: help;
    opacity: 0.6;
    margin-left: 4px;
    font-size: 12px;
}
.note-icon:hover { opacity: 1; }

/* Container name link — subtle hover */
.container-link {
    color: var(--text);
    text-decoration: none;
    transition: color 0.15s;
}
.container-link:hover { color: var(--accent); }
.btn-back {
    color: var(--text-muted);
    text-decoration: none;
    font-size: 13px;
}
.btn-back:hover { color: var(--accent); }

/* ── Maintenance banner ────────────────────────────────────── */
.maint-banner {
    max-width: 900px;
    margin: 12px auto -4px auto;
    padding: 10px 16px;
    background: linear-gradient(180deg, rgba(210,153,34,0.12) 0%, rgba(210,153,34,0.04) 100%);
    border: 1px solid var(--warn);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-size: 14px;
    display: flex;
    align-items: center;
    gap: 10px;
}
.maint-banner strong { color: var(--warn); }
.maint-banner-icon { font-size: 18px; }

/* ── Simple/Advanced UI mode ───────────────────────────────── */
/* Elements marked .adv-only are hidden when body is in simple mode.
   Cards (.card.adv-only) collapse cleanly because display:none removes
   them from layout flow entirely. */
body.mode-simple .adv-only { display: none !important; }
.simple-hint {
    margin: 8px 0 16px 0;
    padding: 10px 14px;
    background: rgba(56,139,253,0.08);
    border: 1px solid rgba(56,139,253,0.4);
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    font-size: 13px;
}
body.mode-advanced .simple-hint { display: none; }

/* ── First-run wizard ──────────────────────────────────────── */
.wizard-head { margin-bottom: 18px; }
.wizard-stepper {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    margin: 12px 0 24px 0;
}
.wstep {
    width: 28px; height: 28px;
    border-radius: 50%;
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text-muted);
    font-size: 13px;
    font-weight: 600;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: all 0.2s;
}
.wstep.is-active {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
    box-shadow: 0 0 0 4px rgba(88,166,255,0.15);
}
.wstep.is-done {
    background: var(--success);
    border-color: var(--success);
    color: #fff;
}
.wstep-bar {
    flex: 0 0 36px;
    height: 2px;
    background: var(--border);
    margin: 0 2px;
}
.wstep-pane { display: none; min-height: 200px; }
.wstep-pane.is-active { display: block; }
.wizard-presets {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 12px;
}
.wizard-preset.is-active {
    background: var(--accent-bg);
    border-color: var(--accent);
    color: var(--accent);
}
.wizard-radio { display: flex; flex-direction: column; gap: 8px; }
.wizard-radio-row {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
}
.wizard-radio-row:hover { border-color: var(--text-muted); }
.wizard-radio-row input[type="radio"] {
    width: auto;
    margin: 4px 0 0 0;
    flex-shrink: 0;
}
.wizard-radio-row input[type="radio"]:checked ~ span {
    color: var(--accent);
}
.wizard-nav {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    margin-top: 24px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
}
.btn-row { display: inline-flex; gap: 4px; align-items: center; flex-wrap: wrap; }
.inline-form { display: inline; }
.btn-compact {
    text-decoration: none;
    font-size: 13px;
    padding: 6px 14px;
}
.card-header-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 14px;
    gap: 8px;
    flex-wrap: wrap;
}
.toolbar-row {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-bottom: 12px;
    flex-wrap: wrap;
}
.search-input {
    flex: 1;
    min-width: 200px;
    margin: 0;
    padding: 6px 12px 6px 32px;
    font-size: 13px;
    background: var(--bg) url("data:image/svg+xml;utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%238b949e' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='11' cy='11' r='8'/%3E%3Cpath d='m21 21-4.3-4.3'/%3E%3C/svg%3E") no-repeat 10px center;
}
.row-info { font-size: 12px; color: var(--text-muted); white-space: nowrap; }
tr.is-hidden { display: none; }

/* ── Mobile responsive ──────────────────────────────────────── */
@media (max-width: 700px) {
    .content { padding: 0 12px; margin: 16px auto; }
    .header { padding: 12px 16px; }
    .card { padding: 16px; }
    .header-brand h1 { font-size: 16px; }
    /* Smaller table cells on mobile to keep more on screen */
    th, td { padding: 8px 6px; font-size: 13px; }
    /* Image column gets clipped on very narrow screens to keep columns aligned */
    .image-cell { max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    /* Bulk bar wraps tighter */
    .bulk-bar { padding: 8px; gap: 4px; }
    .bulk-bar .btn-sm { font-size: 11px; padding: 4px 8px; }
    .bulk-count { min-width: 0; }
    /* Tabs scroll horizontally if too wide */
    .tabs { overflow-x: auto; flex-wrap: nowrap; }
    .tab-btn { white-space: nowrap; }
}
.bulk-cb { width: auto; margin: 0; cursor: pointer; }
.bulk-bar {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    align-items: center;
    margin-bottom: 12px;
    padding: 10px 12px;
    background: var(--bg);
    border: 1px solid var(--border-soft);
    border-radius: var(--radius-sm);
    transition: border-color 0.15s, background 0.15s;
}
.bulk-bar.is-active {
    border-color: var(--accent);
    background: linear-gradient(180deg, rgba(88,166,255,0.04) 0%, var(--bg) 100%);
}
.bulk-count {
    font-size: 12px;
    color: var(--text-muted);
    margin-right: 4px;
    min-width: 110px;
}
.bulk-bar.is-active .bulk-count { color: var(--accent); font-weight: 500; }
.bulk-bar button[disabled] {
    opacity: 0.35;
    cursor: not-allowed;
    filter: saturate(0.3);
    pointer-events: none;
}
.bulk-bar button.is-hidden { display: none; }
.bulk-divider {
    width: 1px;
    height: 18px;
    background: var(--border);
    margin: 0 6px;
}
@media (max-width: 600px) { .bulk-divider { display: none; } }

/* ── Layout helpers ─────────────────────────────────────────── */
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 600px) { .grid { grid-template-columns: 1fr; } }
.stat-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
    margin-bottom: 16px;
}
.stat { text-align: center; padding: 16px; }
.stat .num { font-size: 32px; font-weight: bold; color: var(--accent); line-height: 1.1; }
.stat .label { font-size: 12px; color: var(--text-muted); margin-top: 4px; }

/* ── Badges ─────────────────────────────────────────────────── */
.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: var(--radius-pill);
    font-size: 12px;
}
.badge-green  { background: var(--success-bg); color: var(--success); }
.badge-yellow { background: var(--warn-bg); color: var(--warn); }
.badge-blue   { background: var(--info-bg); color: var(--info); }
.badge-red    { background: var(--danger-bg); color: var(--danger); }
.badge-purple { background: var(--special-bg); color: var(--special); }
.healthy { color: var(--success); }

/* ── Toggle (slider) ────────────────────────────────────────── */
.toggle {
    position: relative;
    display: inline-block;
    width: 36px;
    height: 20px;
    vertical-align: middle;
}
.toggle input { opacity: 0; width: 0; height: 0; }
.toggle .slider {
    position: absolute;
    cursor: pointer;
    inset: 0;
    background: var(--border);
    border-radius: 20px;
    transition: 0.2s;
}
.toggle .slider:before {
    content: "";
    position: absolute;
    height: 14px; width: 14px;
    left: 3px; bottom: 3px;
    background: var(--text-muted);
    border-radius: 50%;
    transition: 0.2s;
}
.toggle input:checked + .slider { background: var(--btn-green); }
.toggle input:checked + .slider:before { transform: translateX(16px); background: #fff; }

/* ── Pre / code ─────────────────────────────────────────────── */
pre {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 16px;
    overflow-x: auto;
    font-size: 13px;
    line-height: 1.5;
    color: var(--text);
    white-space: pre-wrap;
    word-wrap: break-word;
}

/* ── Footer ─────────────────────────────────────────────────── */
.footer {
    text-align: center;
    padding: 24px;
    font-size: 12px;
    color: var(--text-faint);
}
.footer a { color: #6e7681; text-decoration: none; }
.footer a:hover { color: var(--text); }

/* ── Tabs ───────────────────────────────────────────────────── */
.tabs {
    display: flex;
    gap: 4px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 16px;
    flex-wrap: wrap;
}
.tab-btn {
    background: transparent;
    border: none;
    color: var(--text-muted);
    padding: 8px 14px;
    cursor: pointer;
    font-size: 14px;
    font-family: inherit;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    transition: color 0.15s, border-color 0.15s;
    display: inline-flex;
    align-items: center;
    gap: 6px;
}
.tab-btn svg { width: 14px; height: 14px; }
.tab-btn:hover { color: var(--text); }
.tab-btn.is-active {
    color: var(--accent);
    border-bottom-color: var(--accent);
}
.tab-btn[disabled] {
    color: var(--text-faint);
    cursor: not-allowed;
}
.tab-pane { display: none; }
.tab-pane.is-active { display: block; }

/* ── Toasts ─────────────────────────────────────────────────── */
.toast-container {
    position: fixed;
    top: 16px;
    right: 16px;
    z-index: 1000;
    display: flex;
    flex-direction: column;
    gap: 8px;
    max-width: 420px;
}
.toast {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: var(--radius-sm);
    padding: 10px 14px;
    color: var(--text);
    font-size: 14px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.4);
    animation: toastIn 0.2s ease-out;
}
.toast.is-success { border-left-color: var(--success); }
.toast.is-warn    { border-left-color: var(--warn); }
.toast.is-danger  { border-left-color: var(--danger); }
.toast.is-leaving { opacity: 0; transition: opacity 0.3s; }
@keyframes toastIn {
    from { opacity: 0; transform: translateX(8px); }
    to   { opacity: 1; transform: translateX(0); }
}

/* ── Empty states ───────────────────────────────────────────── */
.empty {
    text-align: center;
    padding: 32px 16px;
    color: var(--text-muted);
}
.empty-icon { font-size: 32px; opacity: 0.5; margin-bottom: 8px; }
.empty-title { font-size: 14px; color: var(--text); margin-bottom: 4px; }
.empty-hint { font-size: 12px; color: var(--text-faint); }

/* ── Help icon (tooltip) ────────────────────────────────────── */
.help {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px; height: 14px;
    margin-left: 4px;
    border-radius: 50%;
    background: var(--bg-elev-2);
    color: var(--text-muted);
    font-size: 10px;
    cursor: help;
    position: relative;
}
.help:hover {
    background: var(--accent-bg);
    color: var(--accent);
}
.help[data-tt]:hover::after {
    content: attr(data-tt);
    position: absolute;
    bottom: calc(100% + 6px);
    left: 50%;
    transform: translateX(-50%);
    background: var(--tt-bg);
    color: var(--tt-fg);
    padding: 6px 10px;
    border-radius: var(--radius-sm);
    font-size: 11px;
    white-space: pre-line;
    width: max-content;
    max-width: 280px;
    text-align: left;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
    z-index: 100;
    pointer-events: none;
}

/* ── Confirm dialog (modal) ─────────────────────────────────── */
.modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.7);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 900;
}
.modal-backdrop.is-open { display: flex; }
.modal {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
    max-width: 480px;
    width: 90%;
}
.modal h3 { color: var(--accent); margin-bottom: 12px; }
.modal-body { margin-bottom: 20px; color: var(--text); font-size: 14px; line-height: 1.5; }
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; }
"""


# Vanilla JS shipped on every page — provides:
#   - Tabs (data-tabs / data-tab-target)
#   - Toasts (window.dsToast(msg, kind))
#   - Cross-page toast carryover via localStorage
#   - Confirm dialog (window.dsConfirm(message, onYes))
_BASE_JS = """
(function() {
    // ── Tabs ──────────────────────────────────────────────────
    document.querySelectorAll('[data-tabs]').forEach(function(group) {
        var buttons = group.querySelectorAll('.tab-btn');
        var panes = document.querySelectorAll('[data-tab-pane="' + group.dataset.tabs + '"]');
        function activate(name) {
            buttons.forEach(function(b) { b.classList.toggle('is-active', b.dataset.tabTarget === name); });
            panes.forEach(function(p) { p.classList.toggle('is-active', p.dataset.tabName === name); });
            try { localStorage.setItem('ds-tab-' + group.dataset.tabs, name); } catch(e) {}
        }
        buttons.forEach(function(b) {
            b.addEventListener('click', function() { activate(b.dataset.tabTarget); });
        });
        // Restore from localStorage or default to first tab
        var stored = null;
        try { stored = localStorage.getItem('ds-tab-' + group.dataset.tabs); } catch(e) {}
        var initial = stored && Array.from(buttons).some(function(b){return b.dataset.tabTarget===stored;})
            ? stored
            : (buttons[0] && buttons[0].dataset.tabTarget);
        if (initial) activate(initial);
    });

    // ── Toasts ────────────────────────────────────────────────
    var container = document.getElementById('ds-toasts');
    if (!container) {
        container = document.createElement('div');
        container.id = 'ds-toasts';
        container.className = 'toast-container';
        document.body.appendChild(container);
    }
    window.dsToast = function(msg, kind) {
        var t = document.createElement('div');
        t.className = 'toast' + (kind ? ' is-' + kind : '');
        t.textContent = msg;
        container.appendChild(t);
        setTimeout(function() {
            t.classList.add('is-leaving');
            setTimeout(function() { t.remove(); }, 300);
        }, 4000);
    };
    // Pick up toasts queued from the previous page
    try {
        var queued = JSON.parse(localStorage.getItem('ds-toast-queue') || '[]');
        if (queued.length) {
            queued.forEach(function(item) { window.dsToast(item.msg, item.kind); });
            localStorage.removeItem('ds-toast-queue');
        }
    } catch(e) {}
    // URL-param-driven toast (from server-side redirects)
    var qs = new URLSearchParams(window.location.search);
    if (qs.get('saved') === '1') window.dsToast('Settings saved.', 'success');
    if (qs.get('error'))         window.dsToast(qs.get('error'),    'danger');

    // ── Confirm dialog ────────────────────────────────────────
    var modal = document.getElementById('ds-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'ds-modal';
        modal.className = 'modal-backdrop';
        modal.innerHTML = '<div class="modal" role="dialog" aria-modal="true">' +
            '<h3 id="ds-modal-title">Confirm</h3>' +
            '<div id="ds-modal-body" class="modal-body"></div>' +
            '<div class="modal-actions">' +
            '<button type="button" class="btn-sm btn-outline" id="ds-modal-cancel">Cancel</button>' +
            '<button type="button" class="btn-sm btn" id="ds-modal-ok">Confirm</button>' +
            '</div></div>';
        document.body.appendChild(modal);
    }
    var modalCancel = document.getElementById('ds-modal-cancel');
    var modalOk = document.getElementById('ds-modal-ok');
    var pendingHandler = null;
    function closeModal() {
        modal.classList.remove('is-open');
        pendingHandler = null;
    }
    modalCancel.addEventListener('click', closeModal);
    modal.addEventListener('click', function(e) {
        if (e.target === modal) closeModal();
    });
    modalOk.addEventListener('click', function() {
        var h = pendingHandler;
        closeModal();
        if (h) h();
    });
    window.dsConfirm = function(message, onYes, opts) {
        opts = opts || {};
        document.getElementById('ds-modal-title').textContent = opts.title || 'Confirm';
        document.getElementById('ds-modal-body').textContent = message;
        modalOk.textContent = opts.confirmLabel || 'Confirm';
        modalOk.className = 'btn-sm ' + (opts.danger ? 'btn-danger' : 'btn');
        pendingHandler = onYes;
        modal.classList.add('is-open');
    };
    // ── Theme toggle ──────────────────────────────────────────
    var themeBtn = document.getElementById('ds-theme-toggle');
    var iconDark = document.getElementById('ds-theme-icon-dark');
    var iconLight = document.getElementById('ds-theme-icon-light');
    function applyThemeIcon() {
        var isLight = document.documentElement.getAttribute('data-theme') === 'light';
        // Show the icon for the theme you'd switch *to*
        if (iconDark)  iconDark.style.display  = isLight ? 'block' : 'none';
        if (iconLight) iconLight.style.display = isLight ? 'none'  : 'block';
    }
    applyThemeIcon();
    if (themeBtn) {
        themeBtn.addEventListener('click', function() {
            var current = document.documentElement.getAttribute('data-theme') || 'dark';
            var next = current === 'light' ? 'dark' : 'light';
            if (next === 'light') document.documentElement.setAttribute('data-theme', 'light');
            else document.documentElement.removeAttribute('data-theme');
            try { localStorage.setItem('ds-theme', next); } catch(e) {}
            applyThemeIcon();
        });
    }

    // Auto-wire forms with data-confirm
    document.querySelectorAll('form[data-confirm]').forEach(function(form) {
        form.addEventListener('submit', function(e) {
            if (form.dataset.dsConfirmed === '1') return;
            e.preventDefault();
            window.dsConfirm(form.dataset.confirm, function() {
                form.dataset.dsConfirmed = '1';
                form.submit();
            }, {
                title: form.dataset.confirmTitle || 'Confirm',
                confirmLabel: form.dataset.confirmLabel || 'Confirm',
                danger: form.dataset.confirmDanger === '1',
            });
        });
    });
})();
"""


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
    for name, pattern in zip(field_names, parts):
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


def create_handler(config, checker, bot, store, password=None):
    """Create a request handler with access to app components."""

    # Pre-compute password hash if set
    pw_hash = hashlib.sha256(password.encode()).hexdigest() if password else None

    class WebHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Suppress default logging

        def _check_auth(self):
            """Check Basic Auth if password is configured.

            Uses hmac.compare_digest for the hash comparison to avoid the
            theoretical timing-side-channel that comes with `==` on bytes.
            """
            if not pw_hash:
                return True
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                user, pw = decoded.split(":", 1)
                submitted = hashlib.sha256(pw.encode()).hexdigest()
                return hmac.compare_digest(submitted, pw_hash)
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
            result = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}|{{.Image}}|{{.Status}}"],
                capture_output=True, text=True
            )
            containers = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 2)
                if len(parts) == 3:
                    containers.append({
                        "name": parts[0],
                        "image": parts[1],
                        "status": parts[2],
                    })
            return containers

        def _get_pending(self):
            if os.path.exists(config.pending_file):
                with open(config.pending_file) as f:
                    return json.load(f)
            return []

        def _render_page(self, content, active="status"):
            from i18n import get_translator
            from version import VERSION
            from maintenance import get_state as _maint_state, format_remaining as _maint_remaining
            t = get_translator(config.language)

            nav_items = [
                ("status", f'📊 {t("web_nav_status")}', "/"),
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
<style>{_BASE_CSS}</style>
</head>
<body class="{body_class}">
<div class="header">
<div class="header-row">
<div class="header-brand">
<img src="data:image/png;base64,{_LOGO_B64}" alt="Docksentry">
<h1>Docksentry</h1>
</div>
<div class="header-host-slot"><!-- v2.0: host selector slot --></div>
<form method="POST" action="/api/ui_mode" style="display:inline;margin-left:auto">
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
Docksentry v{VERSION} · <a href="https://github.com/sponsors/amayer1983" target="_blank" rel="noopener noreferrer">❤ Sponsor</a>
</div>
<script>{_BASE_JS}</script>
</body>
</html>"""

        def do_GET(self):
            if not self._check_auth():
                return self._send_auth_required()
            path = self._get_path()
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
            elif path == "/logs":
                self._page_logs()
            elif path == "/settings":
                self._page_settings()
            elif path == "/setup":
                self._page_setup()
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

                # Persist all changes
                config.save_persistent()

                self._send_redirect("/settings?saved=1")
            elif path == "/api/update":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0]
                if name:
                    threading.Thread(target=self._api_update, args=(name,)).start()
                self._send_redirect("/")
            elif path == "/api/pin":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0]
                if name:
                    store.pin(name)
                self._send_redirect("/")
            elif path == "/api/unpin":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0]
                if name:
                    store.unpin(name)
                self._send_redirect("/")
            elif path == "/api/autoupdate":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0]
                if name:
                    store.toggle_auto(name)
                self._send_redirect("/")
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
                name = params.get("name", [""])[0].strip()
                note = params.get("note", [""])[0]
                if name:
                    store.set_note(name, note)
                ref = self.headers.get("Referer", "/")
                ref_path = urlparse(ref).path or "/"
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
            elif path == "/api/group_save":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0].strip()
                # Multi-select: containers come as repeated key in form-encoded
                containers = params.get("containers", [])
                wait_s = params.get("wait_seconds", ["30"])[0]
                if name and containers:
                    # Generate a slug from the name (simple, ascii-safe)
                    import re as _re
                    slug = _re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "group"
                    # If a group with this slug already exists, append -2, -3, ...
                    existing = store.get_groups()
                    base, n = slug, 2
                    while slug in existing:
                        slug = f"{base}-{n}"
                        n += 1
                    store.save_group(slug, name, containers, wait_s)
                self._send_redirect("/settings#groups")
            elif path == "/api/group_delete":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                gid = params.get("group_id", [""])[0].strip()
                if gid:
                    store.delete_group(gid)
                self._send_redirect("/settings#groups")
            elif path == "/api/group_reorder":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                gid = params.get("group_id", [""])[0].strip()
                cname = params.get("container", [""])[0].strip()
                direction = params.get("direction", [""])[0].strip()
                if gid and cname and direction in ("up", "down"):
                    store.reorder_group_container(gid, cname, direction)
                self._send_redirect("/settings#groups")
            elif path == "/api/window":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0].strip()
                action = params.get("action", ["save"])[0]
                if name and action == "delete":
                    store.clear_update_window(name)
                elif name and action == "save":
                    start = params.get("start", [""])[0].strip()
                    end = params.get("end", [""])[0].strip()
                    weekdays = [int(d) for d in params.get("weekdays", [])
                                if d.strip().isdigit()]
                    # Basic validation: HH:MM
                    import re as _re
                    if (_re.match(r"^([01][0-9]|2[0-3]):[0-5][0-9]$", start)
                            and _re.match(r"^([01][0-9]|2[0-3]):[0-5][0-9]$", end)):
                        store.set_update_window(name, start, end, weekdays)
                self._send_redirect("/settings#windows")
            elif path == "/api/ask_major":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0].strip()
                if name:
                    store.toggle_ask_before_major(name)
                self._send_redirect("/")
            elif path == "/api/major_confirm":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0].strip()
                action = params.get("action", [""])[0]
                if name and action == "confirm":
                    threading.Thread(target=bot._confirm_major_update,
                                     args=(checker, name)).start()
                elif name and action == "reject":
                    store.remove_pending_major(name)
                self._send_redirect("/")
            elif path == "/api/bulk":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                action = params.get("action", [""])[0]
                names = params.get("names", [])
                # Form sends a single comma-separated value (from JS join);
                # fall back to multi-value POST if browser sends repeated key.
                if len(names) == 1 and "," in names[0]:
                    names = [n.strip() for n in names[0].split(",") if n.strip()]
                names = [n for n in names if n.strip()]
                if action and names:
                    threading.Thread(
                        target=self._api_bulk, args=(action, names)
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
            from i18n import available_languages, get_translator
            t = get_translator(config.language)

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

        def _page_status(self):
            containers = self._get_containers()
            pending = self._get_pending()
            pending_names = [u["name"] for u in pending]
            pinned = store.get_pinned()
            auto_list = store.get_autoupdate()
            ask_major = store.get_ask_before_major()
            # Build a quick lookup container_name → (group_id, group_name)
            groups_lookup = {}
            for gid, g in store.get_groups().items():
                gname = g.get("name", gid)
                for cname in g.get("containers") or []:
                    groups_lookup[cname] = (gid, gname)
            notes_lookup = store.get_notes()
            major_pending = store.get_pending_major() or {}

            from i18n import get_translator
            t = get_translator(config.language)

            rows = ""
            for c in containers:
                status_text = c["status"]
                if "healthy" in status_text.lower():
                    status_badge = '<span class="badge badge-green">healthy</span>'
                elif "starting" in status_text.lower():
                    status_badge = '<span class="badge badge-yellow">starting</span>'
                else:
                    status_badge = f'<span class="badge badge-blue">running</span>'

                # Badges (compact, only show what's "different" from default)
                badges = ""
                if c["name"] in pending_names:
                    badges += f' <span class="badge badge-yellow" title="{_e(t("web_badge_update_tt"))}">{t("web_badge_update")}</span>'
                if c["name"] in pinned:
                    badges += f' <span class="badge badge-red" title="{_e(t("web_badge_pinned_tt"))}">{t("web_pinned_badge")}</span>'
                if c["name"] in auto_list:
                    badges += f' <span class="badge badge-purple" title="{_e(t("web_badge_auto_tt"))}">{t("web_autoupdate_badge")}</span>'
                if c["name"] in ask_major:
                    badges += f' <span class="badge badge-blue" title="{_e(t("web_badge_major_tt"))}">{_ICONS["alert"]}</span>'
                if c["name"] in groups_lookup:
                    gid, gname = groups_lookup[c["name"]]
                    badges += f' <span class="badge badge-purple" title="{_e(t("web_badge_group_tt", group=gname))}">{_icon_label("package", _e(gname))}</span>'
                if c["name"] in notes_lookup:
                    note_text = notes_lookup[c["name"]]
                    badges += f' <span class="note-icon" title="{_e(note_text)}">📝</span>'

                # Action buttons — icon-only with tooltips. Container name is
                # escaped for safe use in HTML attributes.
                name_attr = _e(c["name"])
                is_auto = c["name"] in auto_list
                is_askm = c["name"] in ask_major
                is_pinned_c = c["name"] in pinned
                update_btn = (
                    f'<form method="POST" action="/api/update" class="inline-form">'
                    f'<input type="hidden" name="name" value="{name_attr}">'
                    f'<button type="submit" class="btn-icon is-active" title="{_e(t("web_update"))}">{_ICONS["refresh"]}</button>'
                    f'</form>'
                ) if c["name"] in pending_names else ''
                pin_form_action = "/api/unpin" if is_pinned_c else "/api/pin"
                pin_btn = (
                    f'<form method="POST" action="{pin_form_action}" class="inline-form">'
                    f'<input type="hidden" name="name" value="{name_attr}">'
                    f'<button type="submit" class="btn-icon{" is-pinned" if is_pinned_c else ""}" '
                    f'title="{_e(t("web_unpin") if is_pinned_c else t("web_pin"))}">{_ICONS["pin"]}</button>'
                    f'</form>'
                )
                auto_btn = (
                    f'<form method="POST" action="/api/autoupdate" class="inline-form adv-only">'
                    f'<input type="hidden" name="name" value="{name_attr}">'
                    f'<button type="submit" class="btn-icon{" is-active" if is_auto else ""}" '
                    f'title="{_e(t("web_autoupdate_disable") if is_auto else t("web_autoupdate_enable"))}">{_ICONS["settings"]}</button>'
                    f'</form>'
                )
                ask_btn = (
                    f'<form method="POST" action="/api/ask_major" class="inline-form adv-only">'
                    f'<input type="hidden" name="name" value="{name_attr}">'
                    f'<button type="submit" class="btn-icon{" is-warn" if is_askm else ""}" '
                    f'title="{_e(t("web_ask_major_off") if is_askm else t("web_ask_major_on"))}">{_ICONS["alert"]}</button>'
                    f'</form>'
                )
                actions = f'<div class="btn-row">{update_btn}{pin_btn}{auto_btn}{ask_btn}</div>'

                rows += f"""<tr>
<td><input type="checkbox" class="bulk-cb" value="{name_attr}" data-pending="{1 if c["name"] in pending_names else 0}" data-pinned="{1 if is_pinned_c else 0}" data-auto="{1 if is_auto else 0}"></td>
<td><a href="/container/{name_attr}" class="container-link">{_e(c['name'])}</a>{badges}</td>
<td class="image-cell"><code>{_e(c['image'])}</code></td>
<td>{status_badge}</td>
<td>{actions}</td>
</tr>"""

            major_banner = ""
            if major_pending:
                rows_mp = ""
                for n, info in major_pending.items():
                    rows_mp += f"""<tr>
<td><span style="color:var(--warn);vertical-align:middle">{_ICONS["alert"]}</span> <code>{_e(n)}</code></td>
<td><code>{_e(info.get('old_version',''))} → {_e(info.get('new_version',''))}</code></td>
<td>
<form method="POST" action="/api/major_confirm" class="inline-form">
<input type="hidden" name="name" value="{_e(n)}">
<input type="hidden" name="action" value="confirm">
<button type="submit" class="btn-sm btn">{t("web_major_confirm")}</button>
</form>
<form method="POST" action="/api/major_confirm" class="inline-form" style="margin-left:6px">
<input type="hidden" name="name" value="{_e(n)}">
<input type="hidden" name="action" value="reject">
<button type="submit" class="btn-sm btn-outline">{t("web_major_reject")}</button>
</form>
</td>
</tr>"""
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
    <div class="num">{len(containers)}</div>
    <div class="label">{t("web_containers")}</div>
</div>
<div class="card stat">
    <div class="num"{' style="color:var(--warn)"' if pending else ''}>{len(pending)}</div>
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
<a href="/api/check" class="btn btn-blue btn-compact btn-icon-text">{_ICONS["search"]}<span>{t("web_check_updates")}</span></a>
</div>
<div class="toolbar-row">
<input type="text" id="containerSearch" class="search-input" placeholder="{_e(t('web_search_placeholder'))}">
<span class="row-info" id="containerCount">{t("web_containers_running", count=len(containers))}</span>
</div>
<form id="bulkForm" method="POST" action="/api/bulk" class="bulk-bar">
<input type="hidden" name="action" id="bulkAction" value="">
<input type="hidden" name="names" id="bulkNames" value="">
<span id="bulkCount" class="bulk-count">{t("web_bulk_none_selected")}</span>
<span class="bulk-divider"></span>
<button type="button" class="btn-sm btn btn-icon-text" onclick="bulkSubmit('update')" title="{_e(t('web_bulk_update_tt'))}">{_ICONS["refresh"]}<span>{t("web_bulk_update")}</span></button>
<button type="button" class="btn-sm btn-outline btn-icon-text" onclick="bulkSubmit('pin')" title="{_e(t('web_bulk_pin_tt'))}">{_ICONS["pin"]}<span>{t("web_bulk_pin")}</span></button>
<button type="button" class="btn-sm btn-outline btn-icon-text" onclick="bulkSubmit('unpin')" title="{_e(t('web_bulk_unpin_tt'))}">{_ICONS["pin"]}<span>{t("web_bulk_unpin")}</span></button>
<button type="button" class="btn-sm btn-outline btn-icon-text adv-only" onclick="bulkSubmit('autoupdate_on')" title="{_e(t('web_bulk_auto_on_tt'))}">{_ICONS["settings"]}<span>{t("web_bulk_auto_on")}</span></button>
<button type="button" class="btn-sm btn-outline btn-icon-text adv-only" onclick="bulkSubmit('autoupdate_off')" title="{_e(t('web_bulk_auto_off_tt'))}">{_ICONS["settings"]}<span>{t("web_bulk_auto_off")}</span></button>
</form>
<table>
<tr><th><input type="checkbox" id="bulkSelectAll" style="width:auto" title="{t("web_bulk_select_all")}"></th><th>{t("web_name")}</th><th>{t("web_image")}</th><th>{t("web_status")}</th><th>{t("web_actions")}</th></tr>
{rows}
</table>
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
                : '{t("web_containers_running_short", count=len(containers))}';
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
</script>"""

            self._send_html(self._render_page(content, "status"))

        def _page_container(self, name):
            """Per-container detail view: Overview / History / Logs / Settings.

            Tabs persist in localStorage so reloading keeps the user on the
            same view. URL is stable: /container/<name>.
            """
            from i18n import get_translator
            t = get_translator(config.language)
            name = name.strip("/")
            if not name:
                self._send_redirect("/")
                return

            # Resolve container info — must exist in `docker ps -a`
            inspect = subprocess.run(
                ["docker", "inspect", name],
                capture_output=True, text=True
            )
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

            # Image size
            size_bytes = 0
            try:
                size_inspect = subprocess.run(
                    ["docker", "image", "inspect", "--format", "{{.Size}}", image],
                    capture_output=True, text=True
                )
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
            is_auto = store.is_auto(name)
            is_askm = store.is_ask_before_major(name)
            window = store.get_update_window(name)

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
                badges.append(f'<span class="badge badge-purple">{t("web_autoupdate_badge")}</span>')
            if is_askm:
                badges.append(f'<span class="badge badge-blue">⚠ major-confirm</span>')
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

            note_text = store.get_note(name)
            note_html = ""
            if note_text:
                note_html = f"""<div style="margin-top:14px;padding:12px;background:var(--bg);border-left:3px solid var(--warn);border-radius:var(--radius-sm)">
<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">📝 {t("web_note_title")}</div>
<div style="font-size:13px;white-space:pre-wrap">{_e(note_text)}</div>
</div>"""

            overview_html = f"""<table>
<tr><td style="width:30%">{t("web_detail_image")}</td><td><code>{_e(image)}</code></td></tr>
<tr><td>{t("web_detail_status")}</td><td>{status_badge} {badges_html}</td></tr>
<tr><td>{t("web_detail_size")}</td><td>{_e(size_str)}</td></tr>
<tr><td>{t("web_detail_created")}</td><td>{_e(created)}</td></tr>
<tr><td>{t("web_detail_started")}</td><td>{_e(started_at)}</td></tr>
{compose_row}
{window_row}
{group_row}
</table>
{note_html}"""

            # ── History tab ──────────────────────────────────────
            if history:
                hist_rows = ""
                for h in reversed(history[-50:]):
                    icon = '✅' if h.get("success") else '❌'
                    # Normalize legacy v1.16.1 calendar glyph (see CHANGELOG v1.16.2)
                    detail = h.get("detail", "").replace("📅", "🗓️")
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
            logs_html = f"""<form method="GET" action="/container/{_e(name)}" style="display:flex;gap:12px;align-items:end;margin-bottom:16px">
<input type="hidden" name="tab" value="logs">
<div style="flex:1">
<label>{t("web_logs_lines")}</label>
<input type="number" name="lines" value="100" min="10" max="500">
</div>
<button type="submit" class="btn btn-blue">{t("web_logs_show")}</button>
</form>"""
            query = parse_qs(urlparse(self.path).query)
            if query.get("tab", [""])[0] == "logs":
                lines = max(10, min(int(query.get("lines", ["100"])[0]), 500))
                logs_result = subprocess.run(
                    ["docker", "logs", "--tail", str(lines), name],
                    capture_output=True, text=True, timeout=10
                )
                output = logs_result.stdout or logs_result.stderr
                if output.strip():
                    logs_html += f'<pre>{html.escape(output.strip())}</pre>'
                else:
                    logs_html += f'<p style="color:var(--text-muted)">No logs found.</p>'

            # ── Settings tab — per-container toggles ─────────────
            window_form = self._container_window_form(t, name, window)
            settings_html = f"""<div class="form-checkbox-row">
  <input type="checkbox" id="cb-detail-auto" {'checked' if is_auto else ''} onchange="document.getElementById('frm-detail-auto').submit()">
  <label for="cb-detail-auto">{t("web_autoupdate_enable")}</label>
</div>
<form id="frm-detail-auto" method="POST" action="/api/autoupdate" class="inline-form">
<input type="hidden" name="name" value="{_e(name)}">
</form>
<p class="form-help">{t("web_detail_auto_hint")}</p>

<div class="form-checkbox-row">
  <input type="checkbox" id="cb-detail-major" {'checked' if is_askm else ''} onchange="document.getElementById('frm-detail-major').submit()">
  <label for="cb-detail-major">{t("web_ask_major_on")}</label>
</div>
<form id="frm-detail-major" method="POST" action="/api/ask_major" class="inline-form">
<input type="hidden" name="name" value="{_e(name)}">
</form>
<p class="form-help">{t("web_detail_major_hint")}</p>

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

        def _page_history(self):
            from i18n import get_translator
            t = get_translator(config.language)

            history = []
            if os.path.exists(config.history_file):
                try:
                    with open(config.history_file) as f:
                        history = json.load(f)
                except (json.JSONDecodeError, IOError):
                    pass

            if not history:
                content = f"""<div class="card">
<h2>{t("web_history")}</h2>
<div class="empty">
  <div class="empty-icon">📋</div>
  <div class="empty-title">{t("web_history_empty")}</div>
  <div class="empty-hint">{t("web_history_empty_hint")}</div>
</div>
</div>"""
            else:
                rows = ""
                for h in reversed(history):
                    icon = '<span class="badge badge-green">✅</span>' if h["success"] else '<span class="badge badge-yellow">❌</span>'
                    # Normalize legacy v1.16.1 calendar glyph (see CHANGELOG v1.16.2)
                    detail = h.get('detail', '').replace('📅', '🗓️')
                    rows += f"""<tr>
<td>{_e(h.get('timestamp', ''))}</td>
<td>{_e(h.get('container', ''))}</td>
<td>{icon}</td>
<td style="font-size:12px">{_e(detail)}</td>
</tr>"""

                content = f"""<div class="card">
<h2>{t("web_history")}</h2>
<table>
<tr><th>{t("web_date")}</th><th>{t("web_name")}</th><th>{t("web_result")}</th><th>{t("web_detail")}</th></tr>
{rows}
</table>
</div>"""

            self._send_html(self._render_page(content, "history"))

        def _page_settings(self):
            from i18n import available_languages, get_translator
            from version import VERSION
            t = get_translator(config.language)

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
      <label>{t("web_language")}</label>
      <select name="language">{lang_options}</select>
    </div>
    <div>
      <label>{t("web_cron_schedule")} {help_(t("web_cron_help"))}</label>
      <input type="text" name="cron_schedule" value="{_e(config.cron_schedule)}">
    </div>
  </div>
  <label>{t("web_excluded")} {help_(t("web_excluded_help"))}</label>
  <input type="text" name="exclude_containers" value="{_e(', '.join(config.exclude_containers))}" placeholder="container1, container2">
  <div class="form-checkbox-row adv-only">
    <input type="checkbox" name="debug" id="cb-debug" {cb(config.debug)}>
    <label for="cb-debug">{t("web_debug_mode")} {help_(t("web_debug_help"))}</label>
  </div>
</div>

<!-- ── Updates ────────────────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="updates">
  <div class="form-checkbox-row">
    <input type="checkbox" name="auto_selfupdate" id="cb-auto-su" {cb(config.auto_selfupdate)}>
    <label for="cb-auto-su">{t("web_auto_selfupdate")} {help_(t("web_auto_selfupdate_help"))}</label>
  </div>
  <p class="form-help">{t("web_updates_tab_hint")}</p>
</div>

<!-- ── Aufräumen ─────────────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="cleanup">
  <div class="form-checkbox-row">
    <input type="checkbox" name="auto_cleanup" id="cb-auto-cl" {cb(config.auto_cleanup)}>
    <label for="cb-auto-cl">{t("web_auto_cleanup")}</label>
  </div>
  <p class="form-help">{t("web_auto_cleanup_hint")}</p>

  <div class="grid adv-only">
    <div>
      <label>{t("web_cleanup_grace_hours")} {help_(t("web_cleanup_grace_hours_hint"))}</label>
      <input type="number" name="cleanup_grace_hours" value="{_e(config.cleanup_grace_hours)}" min="0" max="8760">
    </div>
    <div>
      <label>{t("web_cleanup_backup_days")} {help_(t("web_cleanup_backup_days_hint"))}</label>
      <input type="number" name="cleanup_backup_days" value="{_e(config.cleanup_backup_days)}" min="1" max="365">
    </div>
  </div>
  <div class="form-checkbox-row adv-only">
    <input type="checkbox" name="cleanup_backup_local_only" id="cb-bak-local" {cb(config.cleanup_backup_local_only)}>
    <label for="cb-bak-local">{t("web_cleanup_backup_local_only")}</label>
  </div>
  <p class="form-help adv-only">{t("web_cleanup_backup_local_only_hint")}</p>
</div>

<!-- ── Benachrichtigungen ────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="notifs">
  <div class="grid adv-only">
    <div>
      <label>{t("web_disk_warn_percent")} {help_(t("web_disk_warn_percent_hint"))}</label>
      <input type="number" name="disk_warn_percent" value="{_e(config.disk_warn_percent)}" min="50" max="100">
    </div>
    <div>
      <div class="form-checkbox-row" style="margin-top:24px">
        <input type="checkbox" name="disk_warn_auto_cleanup" id="cb-disk-acl" {cb(config.disk_warn_auto_cleanup)}>
        <label for="cb-disk-acl">{t("web_disk_warn_auto_cleanup")}</label>
      </div>
      <p class="form-help">{t("web_disk_warn_auto_cleanup_hint")}</p>
    </div>
  </div>

  <hr class="section-divider adv-only">

  <div class="grid">
    <div>
      <label>{t("web_quiet_hours_start")}</label>
      <input type="text" name="quiet_hours_start" value="{_e(config.quiet_hours_start)}" placeholder="22:00" pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$|^$">
    </div>
    <div>
      <label>{t("web_quiet_hours_end")}</label>
      <input type="text" name="quiet_hours_end" value="{_e(config.quiet_hours_end)}" placeholder="07:00" pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$|^$">
    </div>
  </div>
  <p class="form-help">{t("web_quiet_hours_hint")}</p>

  <hr class="section-divider adv-only">

  <div class="adv-only">
  <h3 style="font-size:14px;color:var(--accent);margin-bottom:8px">{t("web_weekly_title")}</h3>
  <div class="form-checkbox-row">
    <input type="checkbox" name="weekly_report_enabled" id="cb-weekly" {cb(config.weekly_report_enabled)}>
    <label for="cb-weekly">{t("web_weekly_enable")}</label>
  </div>
  <p class="form-help">{t("web_weekly_hint")}</p>
  <div class="grid">
    <div>
      <label>{t("web_weekly_day")}</label>
      <select name="weekly_report_weekday">
        {''.join(f'<option value="{i}" {"selected" if int(config.weekly_report_weekday or 0)==i else ""}>{name}</option>' for i, name in enumerate([t("web_weekday_mon"), t("web_weekday_tue"), t("web_weekday_wed"), t("web_weekday_thu"), t("web_weekday_fri"), t("web_weekday_sat"), t("web_weekday_sun")]))}
      </select>
    </div>
    <div>
      <label>{t("web_weekly_hour")}</label>
      <input type="number" name="weekly_report_hour" value="{_e(config.weekly_report_hour)}" min="0" max="23">
    </div>
  </div>
  </div>
</div>

<!-- ── Kanäle ────────────────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="channels">
  <div class="adv-only">
    <label>Telegram Topic ID {help_(t("web_topic_id_help"))}</label>
    <input type="text" name="telegram_topic_id" value="{_e(config.telegram_topic_id)}" placeholder="{_e(t('web_topic_id_placeholder'))}">

    <label>{t("web_allowed_users")} {help_(t("web_allowed_users_help"))}</label>
    <input type="text" name="telegram_allowed_users" value="{_e(', '.join(str(u) for u in (config.telegram_allowed_users or [])))}" placeholder="{_e(t('web_allowed_users_placeholder'))}">
  </div>

  <label>Discord Webhook {help_(t("web_discord_help"))}</label>
  <input type="text" name="discord_webhook" value="{_e(config.discord_webhook)}" placeholder="https://discord.com/api/webhooks/...">

  <label>Webhook URL {help_(t("web_webhook_help"))}</label>
  <input type="text" name="webhook_url" value="{_e(config.webhook_url)}" placeholder="https://your-service/webhook">
</div>

<div style="margin-top:16px">
  <button type="submit" class="btn">{t("web_save")}</button>
</div>
</form>
</div>

<div class="card adv-only" id="groups">
<h2>{t("web_groups_title")}</h2>
<p class="card-intro">{t("web_groups_intro")}</p>
{self._groups_html(t)}
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

        def _groups_html(self, t):
            """Render the Container Groups section: list of groups + add form."""
            try:
                containers = self._get_containers()
            except Exception:
                containers = []
            container_names = sorted({c["name"] for c in containers})
            groups = store.get_groups()

            # ── Existing groups ──
            groups_html = ""
            if not groups:
                groups_html = (f'<div class="empty">'
                               f'<div class="empty-icon">📦</div>'
                               f'<div class="empty-title">{t("web_groups_empty")}</div>'
                               f'<div class="empty-hint">{t("web_groups_empty_hint")}</div>'
                               f'</div>')
            else:
                for gid, g in groups.items():
                    rows = ""
                    cnames = g.get("containers") or []
                    for idx, cname in enumerate(cnames):
                        up_disabled = " disabled" if idx == 0 else ""
                        down_disabled = " disabled" if idx == len(cnames) - 1 else ""
                        rows += f"""<tr>
<td><span style="color:var(--text-muted);font-size:11px">#{idx + 1}</span></td>
<td><code>{_e(cname)}</code></td>
<td>
<form method="POST" action="/api/group_reorder" class="inline-form">
<input type="hidden" name="group_id" value="{_e(gid)}">
<input type="hidden" name="container" value="{_e(cname)}">
<input type="hidden" name="direction" value="up">
<button type="submit" class="btn-icon"{up_disabled} title="{_e(t('web_groups_move_up'))}">↑</button>
</form>
<form method="POST" action="/api/group_reorder" class="inline-form" style="margin-left:4px">
<input type="hidden" name="group_id" value="{_e(gid)}">
<input type="hidden" name="container" value="{_e(cname)}">
<input type="hidden" name="direction" value="down">
<button type="submit" class="btn-icon"{down_disabled} title="{_e(t('web_groups_move_down'))}">↓</button>
</form>
</td>
</tr>"""
                    wait_s = int(g.get("wait_seconds", 30) or 30)
                    groups_html += f"""<div class="card" style="background:var(--bg);margin-bottom:12px">
<div class="card-header-row">
<h3 style="font-size:14px;color:var(--accent);margin:0">{_ICONS["package"]} {_e(g.get("name", gid))}
<span style="color:var(--text-muted);font-size:11px;font-weight:400">·  {len(cnames)} {t('web_groups_containers')} · {wait_s}s {t('web_groups_wait')}</span>
</h3>
<form method="POST" action="/api/group_delete" class="inline-form" data-confirm="{_e(t('web_groups_delete_confirm', name=g.get('name', gid)))}" data-confirm-label="{_e(t('web_delete'))}" data-confirm-danger="1">
<input type="hidden" name="group_id" value="{_e(gid)}">
<button type="submit" class="btn-sm btn-outline">{t("web_delete")}</button>
</form>
</div>
<table>{rows}</table>
</div>"""

            # ── Add-new-group form ──
            options = "".join(f'<option value="{_e(n)}">{_e(n)}</option>' for n in container_names)
            return f"""{groups_html}

<div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border)">
<h3 style="font-size:14px;color:var(--accent);margin-bottom:8px">+ {t("web_groups_new")}</h3>
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
{options}
</select>
<button type="submit" class="btn" style="margin-top:8px">{t("web_groups_save")}</button>
</form>
</div>"""

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
            from i18n import get_translator
            t = get_translator(config.language)

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
                result = subprocess.run(
                    ["docker", "logs", "--tail", str(lines), container],
                    capture_output=True, text=True, timeout=10
                )
                output = result.stdout or result.stderr
                if output.strip():
                    log_html = f'<pre>{html.escape(output.strip())}</pre>'
                else:
                    log_html = f'<p style="color:#8b949e">No logs found.</p>'

            content = f"""
<div class="card">
<h2>{t("web_logs")}</h2>
<form method="GET" action="/logs" style="display:flex;gap:12px;align-items:end;margin-bottom:16px">
<div style="flex:1">
<label>Container</label>
<select name="container">{options}</select>
</div>
<div style="width:100px">
<label>{t("web_logs_lines")}</label>
<input type="number" name="lines" value="{lines}" min="10" max="500">
</div>
<button type="submit" class="btn btn-blue" style="height:38px">{t("web_logs_show")}</button>
</form>
{log_html}
</div>"""

            self._send_html(self._render_page(content, "logs"))

        def _api_update(self, name):
            """Trigger update for a single container from Web UI."""
            try:
                if not os.path.exists(config.pending_file):
                    return
                with open(config.pending_file) as f:
                    updates = json.load(f)
                target = next((u for u in updates if u["name"] == name), None)
                if not target:
                    return
                compose_kwargs = {k: target[k] for k in target if k.startswith("compose_")}
                success, msg = checker.update_container(name, target["image"], **compose_kwargs)
                status = "✅" if success else "❌"
                bot.send_message(f"{status} `{name}`: {msg}")
                if bot.notifier:
                    bot.notifier.send_update_result(name, target["image"], success, msg)
                # Remove from pending
                remaining = [u for u in updates if u["name"] != name]
                with open(config.pending_file, "w") as f:
                    json.dump(remaining, f)
            except Exception as e:
                print(f"Web UI update error: {e}")

        def _api_check(self):
            try:
                updates = checker.check_all(bot=bot)
                if updates:
                    bot.notify_updates(updates)
            except Exception as e:
                print(f"Web UI check error: {e}")

        def _api_cleanup(self):
            """Run `docker image prune` to free disk space (manual trigger)."""
            try:
                ok, msg = checker.cleanup_images()
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
                    # Headless variant — reuse the auto-selfupdate path which
                    # already runs without sending Telegram messages.
                    bot.check_selfupdate_auto()
            except Exception as e:
                print(f"Web UI selfupdate error: {e}")
                if bot.notifier and bot.notifier.has_channels():
                    bot.notifier.send_message(f"❌ Selfupdate failed: {e}")

        def _api_bulk(self, action, names):
            """Apply a bulk action to a list of containers.

            Supported actions: pin, unpin, autoupdate_on, autoupdate_off,
            update. Update walks through the pending-updates list and runs
            each matching update sequentially.
            """
            try:
                if action == "pin":
                    for n in names:
                        store.pin(n)
                elif action == "unpin":
                    for n in names:
                        store.unpin(n)
                elif action == "autoupdate_on":
                    auto = store.get_autoupdate()
                    for n in names:
                        if n not in auto:
                            auto.append(n)
                    store.save_autoupdate(auto)
                elif action == "autoupdate_off":
                    auto = store.get_autoupdate()
                    auto = [a for a in auto if a not in names]
                    store.save_autoupdate(auto)
                elif action == "update":
                    if not os.path.exists(config.pending_file):
                        return
                    with open(config.pending_file) as f:
                        updates = json.load(f)
                    targets = [u for u in updates if u["name"] in names]
                    for target in targets:
                        compose_kwargs = {k: target[k] for k in target if k.startswith("compose_")}
                        success, msg = checker.update_container(
                            target["name"], target["image"], **compose_kwargs
                        )
                        status = "✅" if success else "❌"
                        if bot.enabled:
                            bot.send_message(f"{status} `{target['name']}`: {msg}")
                        if bot.notifier:
                            bot.notifier.send_update_result(
                                target["name"], target["image"], success, msg
                            )
                    # Drop processed entries from pending
                    remaining = [u for u in updates if u["name"] not in [t["name"] for t in targets]]
                    with open(config.pending_file, "w") as f:
                        json.dump(remaining, f)
                else:
                    print(f"Web UI bulk: unknown action {action!r}")
            except Exception as e:
                print(f"Web UI bulk error: {e}")

    return WebHandler


class WebUI:
    def __init__(self, config, checker, bot, store, port=8080, password=""):
        self.config = config
        self.port = port
        self.handler = create_handler(config, checker, bot, store, password or None)
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
