"""What a notification SAYS — once, for every channel (#63).

Six channels each wrote their own English for the two structured
notifications, in six slightly different wordings, and two of them tagged
the host while four did not. So an instance set to German announced
updates in German on Telegram and in English everywhere else — content
differing between connections, which is the line this project draws.

The sentences live here now, translated like everything else. A channel
still decides its own bullet character, its own emphasis and whether it
sends HTML — that is presentation, and it stays where it belongs.
"""


def _t(lang):
    from i18n import get_translator
    return get_translator(lang or "en")


def lang_of(channel):
    """The configured language of a channel, defensively.

    Some tests build a notifier with `__new__` and no config at all to
    exercise one pure method; asking such an object for `config.language`
    used to raise. A missing config means English, not a crash — the
    language is the least important thing about a notification.
    """
    return getattr(getattr(channel, "config", None), "language", "en") or "en"


def container_line(u, version_str="", bullet="•"):
    """One container in an update list.

    The host tag is part of it, always: `plex` on the NAS and `plex` at
    home are two containers, and four of the six channels used to leave
    the difference out.
    """
    host = u.get("host") if isinstance(u, dict) else None
    where = f" @{host}" if host and host != "local" else ""
    size = u.get("size", "?")
    created = u.get("created", "?")
    ver = f" {version_str}" if version_str else ""
    return (f"{bullet} {u['name']}{where} ({u['image']}){ver}"
            f" — {size}, {created}")


def updates_available(updates, lang="en", version_of=None, bullet="•"):
    """(title, body) for "these containers have updates"."""
    t = _t(lang)
    title = t("notify_updates_title", count=len(updates))
    lines = [container_line(u, (version_of(u) if version_of else ""), bullet)
             for u in updates]
    return title, t("notify_updates_intro") + "\n\n" + "\n".join(lines)


def update_result(name, image, success, detail="", source_url="", lang="en"):
    """(title, body) for one finished update."""
    t = _t(lang)
    title = t("notify_result_ok" if success else "notify_result_failed",
              name=name)
    body = f"{name} ({image})"
    if detail:
        body += f"\n\n{detail}"
    if source_url:
        body += f"\n\n{source_url}"
    return title, body
