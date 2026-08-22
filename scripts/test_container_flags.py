#!/usr/bin/env python3
"""What the flag commands DO, decided once (#63).

`/pin`, `/unpin`, `/autoupdate`, `/protect`, `/trustrunning`, `/askmajor`,
`/note` and `/cooldown` were written twice — once in the Telegram
dispatcher, once as Discord command methods — and had drifted in four
ways that this module ends, each of which is checked here:

  * Discord had no `@all`: its host resolver never looked for the
    sentinel, so `host: all` came back as "unknown host";
  * Telegram's container resolver had no guard around the backend call,
    so a host refusing the connection raised through the poll loop — the
    user got no answer at all;
  * Telegram parsed `/cooldown`'s number INSIDE the per-host loop, so a
    bad value could answer for the first host and then abort;
  * `/unpin` matches against the stored list rather than against what is
    running — correct in both, by luck rather than by design.

The core returns KEYS, never sentences: that is what lets Telegram answer
per host and Discord join into one clipped blob while the wording stays
in one place.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import container_flags as cf  # noqa: E402
from hosts import ALL_HOSTS  # noqa: E402

checks = {}


class Store:
    def __init__(self, pins=(), auto=(), prot=()):
        self.pins = list(pins)
        self.auto = list(auto)
        self.prot = list(prot)
        self.notes = {}
        self.cool = {}

    def get_pinned(self): return list(self.pins)
    def save_pinned(self, v): self.pins = list(v)
    def get_autoupdate(self): return list(self.auto)

    def toggle_auto(self, n):
        if n in self.auto:
            self.auto.remove(n)
            return False
        self.auto.append(n)
        return True

    def get_protect_stop(self): return list(self.prot)

    def toggle_protect_stop(self, n):
        if n in self.prot:
            self.prot.remove(n)
            return False
        self.prot.append(n)
        return True

    def set_note(self, n, t): self.notes[n] = t
    def get_cooldown(self, n): return self.cool.get(n, 0)

    def set_cooldown(self, n, s):
        v = max(0, min(600, int(s)))          # the real store's clamp
        self.cool[n] = v
        return v


class Backend:
    def __init__(self, names=("web", "db"), boom=False):
        self.names = names
        self.boom = boom

    def run(self, *a, **k):
        if self.boom:
            raise OSError("connection refused")
        return types.SimpleNamespace(returncode=0,
                                     stdout="\n".join(self.names) + "\n",
                                     stderr="")


def one_host(store, backend=None):
    return (lambda h: store), (lambda h: backend or Backend())


# ── the flags ────────────────────────────────────────────────────────
st = Store()
sf, bf = one_host(st)
out = cf.apply_flag(cf.FLAGS["pin"], [None], store_for=sf, backend_for=bf,
                    partial="db")
checks["pinning answers with the added key"] = out.replies[0].key == "pin_added"
checks["…and writes it"] = st.pins == ["db"] and out.changed

out = cf.apply_flag(cf.FLAGS["pin"], [None], store_for=sf, backend_for=bf,
                    partial="db")
checks["pinning twice says it is already pinned"] = (
    out.replies[0].key == "pin_already")
checks["…and writes nothing the second time"] = (
    st.pins == ["db"] and not out.changed)

out = cf.apply_flag(cf.FLAGS["autoupdate"], [None], store_for=sf,
                    backend_for=bf, partial="web")
checks["a toggle reports on"] = out.replies[0].key == "autoupdate_on"
out = cf.apply_flag(cf.FLAGS["autoupdate"], [None], store_for=sf,
                    backend_for=bf, partial="web")
checks["…and off the second time"] = out.replies[0].key == "autoupdate_off"

# ── /unpin resolves against the STORED list, not against `ps` ────────
st2 = Store(pins=["ghost"])                 # not running any more
sf2, bf2 = one_host(st2, Backend(names=("web",)))
out = cf.apply_flag(cf.FLAGS["unpin"], [None], store_for=sf2, partial="ghost")
checks["a stale pin can be lifted after the container is gone"] = (
    out.replies[0].key == "unpin_removed" and st2.pins == [])
out = cf.apply_flag(cf.FLAGS["unpin"], [None], store_for=sf2, partial="web")
checks["…and unpinning something unpinned says so"] = (
    out.replies[0].key == "unpin_not_found" and not out.replies[0].ok)

# ── list mode ────────────────────────────────────────────────────────
st3 = Store(pins=["a", "b"])
out = cf.apply_flag(cf.FLAGS["pin"], [None], store_for=(lambda h: st3),
                    partial=None)
checks["list mode returns the names, sorted"] = (
    out.replies[0].key == "pin_list" and out.replies[0].items == ("a", "b"))
out = cf.apply_flag(cf.FLAGS["pin"], [None], store_for=(lambda h: Store()),
                    partial=None)
checks["…and an empty one says so"] = out.replies[0].key == "pin_empty"

# ── a host that will not answer is reported, not raised ──────────────
st4 = Store()
out = cf.apply_flag(cf.FLAGS["pin"], [None], store_for=(lambda h: st4),
                    backend_for=(lambda h: Backend(boom=True)), partial="web")
checks["a refused host is answered, not raised"] = (
    out.replies[0].key == "chan_list_failed" and not out.replies[0].ok)
checks["…and nothing is written"] = st4.pins == []

# ── cooldown: parsed once, clamped by the store ──────────────────────
st5 = Store()
sf5, bf5 = one_host(st5)
out = cf.set_cooldown([None], store_for=sf5, backend_for=bf5, partial="web",
                      seconds="9999")
checks["a cooldown over the limit is answered as the clamped value"] = (
    out.replies[0].params["seconds"] == 600)
out = cf.set_cooldown([None], store_for=sf5, backend_for=bf5, partial="web",
                      seconds="0")
checks["zero clears it"] = out.replies[0].key == "cooldown_cleared"
out = cf.set_cooldown([None], store_for=sf5, backend_for=bf5, partial="web",
                      seconds=None)
checks["no value shows the current one"] = (
    out.replies[0].key == "cooldown_current")

st6 = Store()
out = cf.set_cooldown([None], store_for=(lambda h: st6),
                      backend_for=(lambda h: Backend()), partial="web",
                      seconds="abc")
checks["a value that will not parse is fatal"] = (
    out.fatal is not None and out.fatal.key == "cooldown_bad_value")
checks["…and writes nothing anywhere, before touching a host"] = (
    st6.cool == {} and out.replies == ())

# ── notes ────────────────────────────────────────────────────────────
st7 = Store()
sf7, bf7 = one_host(st7)
out = cf.set_note([None], store_for=sf7, backend_for=bf7, partial="web",
                  text="GPU box")
checks["a note is set"] = (out.replies[0].key == "note_set"
                           and st7.notes == {"web": "GPU box"})
out = cf.set_note([None], store_for=sf7, backend_for=bf7, partial="web",
                  text="")
checks["…and cleared by an empty one"] = (
    out.replies[0].key == "note_cleared" and st7.notes == {"web": ""})

# ── host targeting, including the @all Discord never had ─────────────
class Host:
    def __init__(self, name, local=False):
        self.name = name
        self.is_local = local


class Registry(list):
    is_multi = True

    @property
    def local(self): return self[0]

    @property
    def names(self): return [h.name for h in self]

    def get(self, n):
        for h in self:
            if h.name == n:
                return h
        return None


reg = Registry([Host("local", True), Host("nas")])
targets, fatal = cf.targets_for_write(reg, None)
checks["no host named means the local one"] = (
    [h.name for h in targets] == ["local"] and fatal is None)
targets, fatal = cf.targets_for_write(reg, "nas")
checks["a named host is the only target"] = [h.name for h in targets] == ["nas"]
targets, fatal = cf.targets_for_write(reg, ALL_HOSTS)
checks["@all reaches every host"] = (
    [h.name for h in targets] == ["local", "nas"])
targets, fatal = cf.targets_for_write(reg, "typo")
checks["an unknown host is fatal, and names itself"] = (
    fatal is not None and fatal.key == "host_unknown"
    and fatal.params["name"] == "typo" and targets == [])

targets, fatal = cf.targets_for_write(None, None)
checks["a single-host install walks the pseudo-host"] = targets == [None]
checks["…which is what keeps its replies untagged"] = (
    cf.apply_flag(cf.FLAGS["pin"], targets, store_for=(lambda h: Store()),
                  backend_for=(lambda h: Backend()),
                  partial="web").replies[0].host_is_local)

# ── per-host isolation: two hosts, two stores ────────────────────────
sa, sb = Store(), Store()
stores = {"local": sa, "nas": sb}
out = cf.apply_flag(cf.FLAGS["pin"], [reg[1]],
                    store_for=(lambda h: stores[h.name]),
                    backend_for=(lambda h: Backend()), partial="web")
checks["pinning on one host does not touch the other"] = (
    sb.pins == ["web"] and sa.pins == [])
checks["…and the reply carries which host it was"] = (
    out.replies[0].host.name == "nas"
    and out.replies[0].host_is_local is False)

# ── the core hands back keys, never sentences ────────────────────────
import json  # noqa: E402
en = json.load(open(os.path.join(os.path.dirname(__file__), "..", "app",
                                 "lang", "en.json"), encoding="utf-8"))
src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "container_flags.py"), encoding="utf-8").read()
import re  # noqa: E402
keys = set(re.findall(r'k_\w+="([a-z_]+)"', src))
keys |= set(re.findall(r'Reply\("([a-z_]+)"', src))
missing = sorted(k for k in keys if k and k not in en)
checks["every key the core names exists"] = not missing
if missing:
    print("  → fehlt in en.json: " + ", ".join(missing))

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
