#!/usr/bin/env python3
"""Weekly summary report.

Aggregates the last 7 days from update_history.json + a few quick checks
on the live system, then sends a single nicely-formatted message to all
configured channels (Telegram, Discord, generic webhook).

The scheduler invokes `maybe_send_weekly_report(...)` on every cron tick.
A small state file (`weekly_report_state.json`) records the last send time
to prevent duplicates within the same day.
"""

import json
import os
import shutil
from datetime import datetime, timedelta


def _load_history(history_file):
    if not os.path.exists(history_file):
        return []
    try:
        with open(history_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _parse_ts(ts):
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def collect_stats(config):
    """Aggregate stats for the last 7 days. Returns a dict ready to format."""
    history = _load_history(config.history_file)
    cutoff = datetime.now() - timedelta(days=7)

    recent = []
    for h in history:
        ts = _parse_ts(h.get("timestamp", ""))
        if ts and ts >= cutoff:
            recent.append(h)

    successes = [h for h in recent if h.get("success")]
    failures = [h for h in recent if not h.get("success")]

    rolled_back = [h for h in failures if "rolled back" in (h.get("detail", "") or "").lower()]
    by_container = {}
    for h in successes:
        n = h.get("container", "?")
        by_container[n] = by_container.get(n, 0) + 1
    top_movers = sorted(by_container.items(), key=lambda kv: -kv[1])[:5]

    # Disk usage right now
    try:
        usage = shutil.disk_usage(config.data_dir)
        disk_pct = round(usage.used * 100 / usage.total, 1) if usage.total else 0
        disk_free_gb = usage.free / 1024**3
    except OSError:
        disk_pct = 0.0
        disk_free_gb = 0.0

    return {
        "period_days": 7,
        "total": len(recent),
        "successes": len(successes),
        "failures": len(failures),
        "rolled_back": len(rolled_back),
        "by_container": by_container,
        "top_movers": top_movers,
        "disk_percent": disk_pct,
        "disk_free_gb": disk_free_gb,
    }


def format_message_text(stats, t):
    """Render the report as Markdown for Telegram + plain for webhooks."""
    free_gb_str = f"{stats['disk_free_gb']:.0f}"
    lines = [
        f"*{t('weekly_report_title')}*",
        f"_{t('weekly_report_period', days=stats['period_days'])}_",
        "",
        f"✅ {t('weekly_report_successes', n=stats['successes'])}",
        f"❌ {t('weekly_report_failures',  n=stats['failures'])}",
    ]
    if stats["rolled_back"]:
        lines.append(f"↩️ {t('weekly_report_rolled_back', n=stats['rolled_back'])}")
    lines.append(f"💾 {t('weekly_report_disk', percent=stats['disk_percent'], free=free_gb_str)}")

    if stats["top_movers"]:
        lines.append("")
        lines.append(f"*{t('weekly_report_top')}*")
        for name, count in stats["top_movers"]:
            lines.append(f"  • `{name}` — {count}×")

    return "\n".join(lines)


def format_discord_embed(stats, t):
    """Discord-rich-embed payload."""
    fields = [
        {"name": t("weekly_report_successes_label"), "value": str(stats["successes"]), "inline": True},
        {"name": t("weekly_report_failures_label"),  "value": str(stats["failures"]),  "inline": True},
    ]
    if stats["rolled_back"]:
        fields.append({"name": t("weekly_report_rolled_back_label"),
                       "value": str(stats["rolled_back"]), "inline": True})
    fields.append({"name": t("weekly_report_disk_label"),
                   "value": f"{stats['disk_percent']}% ({stats['disk_free_gb']:.0f} GB free)",
                   "inline": True})

    if stats["top_movers"]:
        movers_text = "\n".join(f"• **{n}** — {c}×" for n, c in stats["top_movers"])
        fields.append({"name": t("weekly_report_top"), "value": movers_text, "inline": False})

    return {
        "title": t("weekly_report_title"),
        "description": t("weekly_report_period", days=stats["period_days"]),
        "color": 0x58a6ff,
        "fields": fields,
        "footer": {"text": "Docksentry"},
    }


def _read_state(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(path, state):
    """Atomic — see container_store.atomic_write_json (v1.22.1 extends
    coverage to every JSON write site).
    """
    from container_store import atomic_write_json
    try:
        atomic_write_json(path, state)
    except OSError:
        # weekly-report state is purely cosmetic (last-sent timestamp);
        # silent fail is acceptable as before.
        try:
            os.unlink(path + ".tmp")
        except OSError:
            pass


def should_send_now(config, now=None):
    """Decide whether the scheduler should fire the report right now.

    Sends when:
      - feature is enabled
      - current weekday matches config.weekly_report_weekday
      - current hour is at or just after config.weekly_report_hour
      - we haven't already sent today

    Returns True iff the caller should proceed with sending. The state file
    is *not* updated here — the caller updates it after a successful send,
    so a network failure leaves the day open for retry.
    """
    if not getattr(config, "weekly_report_enabled", False):
        return False
    now = now or datetime.now()
    try:
        target_wd = int(config.weekly_report_weekday or 0) % 7
        target_h = int(config.weekly_report_hour or 9) % 24
    except (TypeError, ValueError):
        return False
    if now.weekday() != target_wd:
        return False
    # Fire from the configured hour onwards (in case the scheduler was off
    # at the exact minute) — but only once per day.
    if now.hour < target_h:
        return False
    state = _read_state(config.weekly_report_state_file)
    last = state.get("last_sent", "")
    if last == now.date().isoformat():
        return False
    return True


def mark_sent(config, now=None):
    """Record a successful send. Call only after at least one channel
    accepted the payload."""
    now = now or datetime.now()
    _write_state(config.weekly_report_state_file, {
        "last_sent": now.date().isoformat(),
        "ts": now.isoformat(timespec="seconds"),
    })


def maybe_send_weekly_report(config, bot, t):
    """One-call helper for the scheduler. Returns True if a report was sent.

    Picks Discord embed format for Discord; plain Markdown text for Telegram
    and generic webhook (which receives the raw text in event=weekly_report).
    """
    if not should_send_now(config):
        return False

    stats = collect_stats(config)
    text = format_message_text(stats, t)
    sent_any = False

    if bot is not None and getattr(bot, "enabled", False):
        try:
            bot.send_message(text, auto=True)
            sent_any = True
        except Exception as e:
            print(f"Weekly report (telegram) error: {e}")

    notifier = getattr(bot, "notifier", None) if bot else None
    if notifier is not None:
        # Through the facade, so every channel that is switched on gets it
        # and no channel that is switched off does.
        #
        # This used to be a hand-written list — the Discord webhook and the
        # generic webhook, and from 2.7.1 the bot channel. Two things were
        # wrong with that. E-mail, ntfy, Gotify, Matrix and Apprise had
        # never received a weekly report at all; and the list asked whether
        # a webhook URL was *set*, not whether the channel was *on*, so a
        # switched-off Discord webhook still got one. That is what made the
        # report arrive twice for @NotRetarded (#59) while the "both Discord
        # paths are on" warning stayed hidden — the warning is about the
        # switches, and the switches were exactly what this ignored.
        try:
            if notifier.send_weekly_report(stats, text,
                                           format_discord_embed(stats, t)):
                sent_any = True
        except Exception as e:
            print(f"Weekly report (notifier) error: {e}")

    if sent_any:
        mark_sent(config)
    return sent_any
