#!/usr/bin/env python3
"""Multi-channel notification dispatcher (Discord, Webhook, e-mail/SMTP)."""

import json
import socket
import time
import urllib.error
import urllib.request

from quiet_hours import is_quiet_now


class Notifier:
    """Sends notifications to Discord, generic webhooks, and/or e-mail."""

    def __init__(self, config):
        self.config = config

    def _smtp_configured(self):
        """E-mail is active once host + from + to are all set (#2)."""
        c = self.config
        return bool(c.smtp_host and c.smtp_from and c.smtp_to)

    def has_channels(self):
        """Check if any notification channels are configured."""
        return bool(self.config.discord_webhook or self.config.webhook_url
                    or self._smtp_configured())

    def _suppressed(self):
        """True if quiet-hours OR maintenance is active right now — skip
        auto-notifications. Manual sends still go through (the caller would
        use a different code path for those)."""
        if is_quiet_now(self.config):
            return True
        try:
            from maintenance import is_active as _maint_active
            if _maint_active(self.config):
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _version_str(u):
        """`v_old → v_new` when both are known and differ, else the single
        known version, else "". Mirrors the Telegram badge (#44) so Discord /
        webhook / e-mail show the same version info."""
        old = (u.get("old_version") or "").strip()
        new = (u.get("new_version") or "").strip()
        if old and new and old != new:
            return f"v{old} → v{new}"
        v = old or new
        return f"v{v}" if v else ""

    def send_updates_available(self, updates):
        """Notify about available updates."""
        if self._suppressed():
            return
        if self.config.discord_webhook:
            self._discord_updates(updates)
        if self.config.webhook_url:
            self._webhook_send("updates_available", {
                "count": len(updates),
                "containers": [
                    {"name": u["name"], "image": u["image"],
                     "size": u.get("size", "?"), "created": u.get("created", "?"),
                     "compose": bool(u.get("compose_project")),
                     # Version info (#44) — read from OCI image.version
                     # labels (old=local, new=remote). Empty when the image
                     # doesn't carry the label.
                     "old_version": u.get("old_version", ""),
                     "new_version": u.get("new_version", ""),
                     # Repo / changelog URL — auto-detected from OCI
                     # labels or manually overridden in the Web UI
                     # (#20). Empty string when no link is available.
                     "source_url": u.get("source_url", "")}
                    for u in updates
                ],
            })
        if self._smtp_configured():
            lines = [f"- {u['name']} ({u['image']})"
                     + (f" {self._version_str(u)}" if self._version_str(u) else "")
                     + f" — {u.get('size','?')}, {u.get('created','?')}"
                     for u in updates]
            self._smtp_send(f"{len(updates)} Docker update(s) available",
                            "Docksentry found updates for:\n\n" + "\n".join(lines))

    def send_update_result(self, name, image, success, detail="", source_url=""):
        """Notify about a completed update (success or failure).

        ``source_url`` is the resolved repo / changelog link for the
        container (manual override → OCI label → registry fallback,
        resolved once by the caller via
        ``TelegramBot._enrich_with_source_url``). When present, the
        Discord embed wraps the container name as a clickable link
        and the generic webhook payload carries the URL alongside
        the other fields. Closes a parity gap reported by @NotRetarded
        in #2 — v1.19.2 fixed the Telegram side, this fixes Discord
        and the webhook payload.
        """
        if self._suppressed():
            return
        if self.config.discord_webhook:
            self._discord_update_result(name, image, success, detail, source_url)
        if self.config.webhook_url:
            self._webhook_send("update_result", {
                "container": name,
                "image": image,
                "success": success,
                "detail": detail,
                "source_url": source_url,
            })
        if self._smtp_configured():
            status = "OK" if success else "FAILED"
            body = f"{name} ({image})\n\n{detail}"
            if source_url:
                body += f"\n\n{source_url}"
            self._smtp_send(f"Update {status}: {name}", body)

    def send_message(self, text):
        """Send a plain text notification (subject to quiet hours)."""
        if self._suppressed():
            return
        if self.config.discord_webhook:
            self._discord_message(text)
        if self.config.webhook_url:
            self._webhook_send("message", {"text": text})
        if self._smtp_configured():
            # Strip Telegram *bold* markers for a clean plain-text mail.
            self._smtp_send("Docksentry", text.replace("*", ""))

    # ── Discord ──────────────────────────────────────────────

    @staticmethod
    def _post_json_with_retry(url, payload, headers, channel):
        """POST a JSON body with bounded retry for transient network failures
        (timeout / connection error) — 3 attempts, 2s and 4s backoff. Same
        rationale as the Telegram retry in v1.38.1: right after a self-update
        restart the network can still be settling, and a single dropped
        notification is worse than a rare duplicate. HTTP status codes (2xx /
        4xx) return on the first attempt — retry only covers real network
        errors, not user config problems.

        `channel` is a label used only in the error log ("Discord webhook",
        "Webhook") so failures stay distinguishable.
        """
        data = json.dumps(payload).encode()
        merged = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(url, data=data, headers=merged, method="POST")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return resp.status
            except urllib.error.HTTPError as e:
                # Server responded (4xx / 5xx) — not a transient network blip,
                # don't retry. The retry loop is meant for the case where the
                # request never reached the server.
                print(f"{channel} error: HTTP {e.code}")
                return None
            except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                print(f"{channel} error: {e}")
                return None
            except Exception as e:
                print(f"{channel} error: {e}")
                return None
        return None

    def _discord_post(self, payload):
        """POST JSON to Discord webhook."""
        return self._post_json_with_retry(
            self.config.discord_webhook, payload,
            {"User-Agent": "Docksentry/1.0"}, "Discord webhook")

    def _footer_text(self):
        """Discord-embed footer text. Includes BOT_LABEL when set so
        multiple Docksentry instances posting into the same Discord
        channel can be told apart (e.g. 'Docksentry · pve1')."""
        label = (self.config.bot_label or "").strip()
        return f"Docksentry · {label}" if label else "Docksentry"

    def _discord_updates(self, updates):
        """Send update notification as Discord embed."""
        fields = []
        for u in updates:
            compose_tag = " 🐳" if u.get("compose_project") else ""
            # Discord embed fields don't render links in `name`, but
            # `value` is full markdown — append a clickable
            # "[Source ↗](url)" line when we have a source URL (#20).
            link_line = ""
            if u.get("source_url"):
                link_line = f"\n[Source ↗]({u['source_url']})"
            ver = self._version_str(u)
            ver_line = f"\n🔖 {ver}" if ver else ""
            fields.append({
                "name": f"📦 {u['name']}{compose_tag}",
                "value": f"`{u['image']}`{ver_line}\n📦 {u.get('size', '?')} · 🗓️ {u.get('created', '?')}{link_line}",
                "inline": True,
            })

        label = (self.config.bot_label or "").strip()
        title_prefix = f"{label} · " if label else ""
        embed = {
            "title": f"{title_prefix}🔄 Docker Updates Available ({len(updates)})",
            "color": 0x58a6ff,  # Blue
            "fields": fields,
            "footer": {"text": self._footer_text()},
        }
        self._discord_post({"embeds": [embed]})

    def _discord_update_result(self, name, image, success, detail, source_url=""):
        """Send update result as Discord embed."""
        label = (self.config.bot_label or "").strip()
        title_prefix = f"{label} · " if label else ""
        # Discord embed `description` is full markdown — render the
        # container name as a clickable [name](url) when we have a
        # source URL (matches the "Updates Available" embed already
        # does this for fields, and the Telegram side does it for
        # both pre/post-update message types since v1.19.2).
        name_md = f"[**{name}**]({source_url})" if source_url else f"**{name}**"
        if success:
            embed = {
                "title": f"{title_prefix}✅ Update Successful",
                "description": f"{name_md} (`{image}`)\n{detail}",
                "color": 0x3fb950,  # Green
                "footer": {"text": self._footer_text()},
            }
        else:
            embed = {
                "title": f"{title_prefix}❌ Update Failed",
                "description": f"{name_md} (`{image}`)\n{detail}",
                "color": 0xf85149,  # Red
                "footer": {"text": self._footer_text()},
            }
        self._discord_post({"embeds": [embed]})

    def _discord_message(self, text):
        """Send plain text to Discord."""
        # Strip Markdown bold (*text*) for Discord
        clean = text.replace("*", "**")
        label = (self.config.bot_label or "").strip()
        if label:
            clean = f"**{label}** · {clean}"
        self._discord_post({"content": clean})

    # ── Generic Webhook ──────────────────────────────────────

    def _webhook_send(self, event, data):
        """POST JSON to generic webhook URL."""
        payload = {
            "event": event,
            "source": "docksentry",
            **data,
        }
        # Add bot label to the payload when set so downstream automations
        # (Home Assistant, Ntfy, custom scripts) can route per-host.
        label = (self.config.bot_label or "").strip()
        if label:
            payload["bot_label"] = label
        # Same retry contract as Discord and Telegram — a transient blip after
        # a self-update restart shouldn't drop a notification. Note the
        # trade-off is slightly different for a generic webhook: it may point
        # at a user automation (Home Assistant, ntfy, custom script), so a
        # duplicate could double-trigger something. Documented in the README.
        return self._post_json_with_retry(
            self.config.webhook_url, payload, None, "Webhook")

    # ── E-mail / SMTP ────────────────────────────────────────

    def _smtp_send(self, subject, body):
        """Send a plain-text e-mail via SMTP. `smtp_tls` selects the
        transport: "starttls" (default, 587), "ssl" (implicit, 465) or
        "none". SMTP_TO may be a comma/semicolon-separated list. Best-effort:
        logs and returns on any failure, never raising into the caller —
        same contract as the Discord/webhook channels."""
        import smtplib
        from email.message import EmailMessage
        c = self.config
        recipients = [r.strip() for r in c.smtp_to.replace(";", ",").split(",") if r.strip()]
        if not recipients:
            return
        label = (c.bot_label or "").strip()
        msg = EmailMessage()
        msg["Subject"] = (f"[{label}] " if label else "") + subject
        msg["From"] = c.smtp_from
        msg["To"] = ", ".join(recipients)
        msg.set_content(body)
        try:
            if c.smtp_tls == "ssl":
                server = smtplib.SMTP_SSL(c.smtp_host, c.smtp_port, timeout=15)
            else:
                server = smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=15)
            try:
                if c.smtp_tls == "starttls":
                    server.starttls()
                if c.smtp_user:
                    server.login(c.smtp_user, c.smtp_password)
                server.send_message(msg, to_addrs=recipients)
            finally:
                server.quit()
        except Exception as e:
            print(f"SMTP error: {e}")
