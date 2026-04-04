#!/usr/bin/env python3
"""Telegram Bot - handles messages, callbacks, and notifications."""

import json
import subprocess
import os
import sys
import threading
import urllib.request
import urllib.parse


class TelegramBot:
    def __init__(self, config):
        self.config = config
        self.running = True
        self.update_running = False
        from i18n import get_translator
        self.t = get_translator(config.language)

    def stop(self):
        self.running = False

    def _get_pinned(self):
        if os.path.exists(self.config.pinned_file):
            try:
                with open(self.config.pinned_file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return []

    def _save_pinned(self, pinned):
        with open(self.config.pinned_file, "w") as f:
            json.dump(pinned, f)

    def _get_autoupdate(self):
        if os.path.exists(self.config.autoupdate_file):
            try:
                with open(self.config.autoupdate_file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return []

    def _save_autoupdate(self, containers):
        with open(self.config.autoupdate_file, "w") as f:
            json.dump(containers, f)

    def _resolve_container(self, partial):
        """Resolve a partial container name. Returns (full_name, error_msg)."""
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True
        )
        all_names = [n.strip() for n in result.stdout.strip().split("\n") if n.strip()]

        # Exact match first
        if partial in all_names:
            return partial, None

        # Partial match (starts with)
        matches = [n for n in all_names if n.lower().startswith(partial.lower())]
        if len(matches) == 1:
            return matches[0], None
        elif len(matches) > 1:
            return None, self.t("resolve_multiple", names=", ".join(f"`{m}`" for m in matches))
        else:
            return None, self.t("resolve_not_found", name=partial)

    def api_call(self, method, data=None):
        url = f"https://api.telegram.org/bot{self.config.bot_token}/{method}"
        if data:
            req = urllib.request.Request(
                url,
                data=urllib.parse.urlencode(data).encode(),
                method="POST"
            )
        else:
            req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"Telegram API error: {e}")
            return None

    def send_message(self, text, reply_markup=None):
        data = {
            "chat_id": self.config.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": "true"
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        return self.api_call("sendMessage", data)

    def answer_callback(self, callback_id, text):
        self.api_call("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": text
        })

    def remove_buttons(self, chat_id, message_id):
        self.api_call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": json.dumps({"inline_keyboard": []})
        })

    def _remove_single_button(self, chat_id, message_id, callback_data):
        """Mark clicked button as done, keep remaining buttons."""
        keyboard = self._rebuild_keyboard_without(callback_data)
        self.api_call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": json.dumps(keyboard)
        })

    def _rebuild_keyboard_without(self, callback_data):
        """Rebuild keyboard marking the clicked container as done."""
        if not os.path.exists(self.config.pending_file):
            return {"inline_keyboard": []}

        with open(self.config.pending_file) as f:
            updates = json.load(f)

        keyboard = []
        for u in updates:
            btn_data = f"update_one:{u['name']}"
            if btn_data == callback_data:
                keyboard.append([{"text": f"✅ {u['name']}", "callback_data": "noop"}])
            else:
                keyboard.append([{"text": f"🔄 {u['name']}", "callback_data": btn_data}])

        remaining = [u for u in updates if f"update_one:{u['name']}" != callback_data]
        if remaining:
            keyboard.append([
                {"text": self.t("update_all_btn"), "callback_data": "update_all"},
                {"text": self.t("manual_btn"), "callback_data": "update_skip"}
            ])

        return {"inline_keyboard": keyboard}

    def _run_single_update(self, checker, container_name):
        """Update a single container."""
        if not os.path.exists(self.config.pending_file):
            self.send_message(self.t("no_pending_updates"))
            return

        with open(self.config.pending_file) as f:
            updates = json.load(f)

        target = next((u for u in updates if u["name"] == container_name), None)
        if not target:
            self.send_message(self.t("container_not_in_list", name=container_name))
            return

        self.send_message(self.t("update_single_starting", name=container_name))

        try:
            compose_kwargs = {k: target[k] for k in target if k.startswith("compose_")}
            success, msg = checker.update_container(target["name"], target["image"], **compose_kwargs)
            status = "✅" if success else "❌"
            self.send_message(f"{status} `{container_name}`: {msg}")
        except Exception as e:
            self.send_message(f"❌ `{container_name}`: {str(e)[:200]}")

        # Remove from pending list
        remaining = [u for u in updates if u["name"] != container_name]
        with open(self.config.pending_file, "w") as f:
            json.dump(remaining, f)

        if not remaining:
            self.send_message(self.t("update_all_done"))

    def handle_autoupdates(self, updates, checker):
        """Split updates into auto-update and manual, handle accordingly."""
        auto_list = self._get_autoupdate()
        auto_updates = [u for u in updates if u["name"] in auto_list]
        manual_updates = [u for u in updates if u["name"] not in auto_list]

        # Auto-update containers silently
        if auto_updates:
            self.send_message(self.t("autoupdate_running", count=len(auto_updates)))
            results = []
            for u in auto_updates:
                try:
                    compose_kwargs = {k: u[k] for k in u if k.startswith("compose_")}
                    success, msg = checker.update_container(u["name"], u["image"], **compose_kwargs)
                    status = "✅" if success else "❌"
                    results.append(f"{status} `{u['name']}`: {msg}")
                except Exception as e:
                    results.append(f"❌ `{u['name']}`: {str(e)[:200]}")
            self.send_message(self.t("autoupdate_done") + "\n\n" + "\n".join(results))

            # Remove auto-updated from pending
            remaining = [u for u in updates if u["name"] not in [a["name"] for a in auto_updates]]
            with open(self.config.pending_file, "w") as f:
                json.dump(remaining, f)

        # Notify about remaining manual updates
        if manual_updates:
            self.notify_updates(manual_updates)

    def notify_updates(self, updates):
        if not updates:
            return
        names = []
        for u in updates:
            size = u.get('size', '?')
            created = u.get('created', '?')
            compose_tag = " 🐳" if u.get("compose_project") else ""
            names.append(f"• `{u['name']}` ({u['image']}){compose_tag}\n  📦 {size} | 📅 {self.t('current')}: {created}")
        text = self.t("updates_available") + "\n\n" + "\n".join(names)

        # One button per container + all/skip at the bottom
        keyboard = []
        for u in updates:
            size = u.get('size', '?')
            keyboard.append([
                {"text": f"🔄 {u['name']} ({size})", "callback_data": f"update_one:{u['name']}"}
            ])
        keyboard.append([
            {"text": self.t("update_all_btn"), "callback_data": "update_all"},
            {"text": self.t("manual_btn"), "callback_data": "update_skip"}
        ])

        reply_markup = {"inline_keyboard": keyboard}
        self.send_message(text, reply_markup)

    def notify_no_updates(self):
        self.send_message(self.t("all_up_to_date"))

    def _handle_selfupdate(self):
        """Pull latest image and recreate own container."""
        hostname = os.environ.get("HOSTNAME", "")
        if not hostname:
            self.send_message(self.t("selfupdate_failed_id"))
            return

        # Get own container info
        result = subprocess.run(
            ["docker", "inspect", hostname],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            self.send_message(self.t("selfupdate_failed_container"))
            return

        config = json.loads(result.stdout)[0]
        own_name = config["Name"].lstrip("/")
        own_image = config["Config"]["Image"]

        # Get current image info
        old_created = config.get("Created", "")[:10]
        old_id_short = config["Image"][:19]

        self.send_message(
            self.t("selfupdate_checking", image=own_image) + "\n"
            + self.t("selfupdate_current_version", date=old_created) + "\n"
            + self.t("selfupdate_image_id", id=old_id_short)
        )

        # Pull latest
        pull = subprocess.run(
            ["docker", "pull", own_image],
            capture_output=True, text=True, timeout=300
        )
        if pull.returncode != 0:
            self.send_message(self.t("selfupdate_failed_pull", error=pull.stderr[:200]))
            return

        # Check if image actually changed
        new_inspect = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}||{{.Created}}", own_image],
            capture_output=True, text=True
        )
        parts = new_inspect.stdout.strip().split("||")
        new_id = parts[0]
        new_created = parts[1][:10] if len(parts) > 1 else "?"
        old_id = config["Image"]

        if new_id == old_id:
            self.send_message(self.t("selfupdate_up_to_date"))
            return

        new_id_short = new_id[:19]
        self.send_message(
            self.t("selfupdate_found") + "\n"
            + self.t("selfupdate_dates", new=new_created, old=old_created) + "\n"
            + self.t("selfupdate_ids", old=old_id_short, new=new_id_short) + "\n\n"
            + self.t("selfupdate_restarting")
        )

        self._do_selfupdate(config, own_name, own_image)

    def _do_selfupdate(self, config, own_name, own_image):
        """Execute selfupdate: rebuild run command from inspect, stop, recreate, exit."""
        # Create update script that runs after this container stops
        update_cmd = (
            f"docker stop {own_name} && "
            f"docker rename {own_name} {own_name}_old && "
            f"docker run -d "
        )

        # Rebuild run command from inspect
        run_args = []

        run_args.extend(["--name", own_name])

        # Restart policy
        restart = config.get("HostConfig", {}).get("RestartPolicy", {})
        if restart.get("Name"):
            policy = restart["Name"]
            if restart.get("MaximumRetryCount", 0) > 0:
                policy += f":{restart['MaximumRetryCount']}"
            run_args.extend(["--restart", policy])

        # Network
        network_mode = config.get("HostConfig", {}).get("NetworkMode", "")
        if network_mode and network_mode != "default":
            run_args.extend(["--network", network_mode])

        # Env vars
        for env in config.get("Config", {}).get("Env", []):
            run_args.extend(["-e", env])

        # Mounts
        for mount in config.get("Mounts", []):
            if mount["Type"] == "bind":
                bind = f"{mount['Source']}:{mount['Destination']}"
                if not mount.get("RW", True):
                    bind += ":ro"
                run_args.extend(["-v", bind])
            elif mount["Type"] == "volume":
                bind = f"{mount['Name']}:{mount['Destination']}"
                if not mount.get("RW", True):
                    bind += ":ro"
                run_args.extend(["-v", bind])

        # Ports
        ports = config.get("HostConfig", {}).get("PortBindings", {}) or {}
        for container_port, bindings in ports.items():
            if bindings:
                for b in bindings:
                    host_ip = b.get("HostIp", "")
                    host_port = b.get("HostPort", "")
                    if host_ip:
                        run_args.extend(["-p", f"{host_ip}:{host_port}:{container_port}"])
                    else:
                        run_args.extend(["-p", f"{host_port}:{container_port}"])

        # Labels
        for key, value in config.get("Config", {}).get("Labels", {}).items():
            run_args.extend(["--label", f"{key}={value}"])

        # Security opts
        for opt in config.get("HostConfig", {}).get("SecurityOpt", []) or []:
            run_args.extend(["--security-opt", opt])

        # Build full command
        run_parts = " ".join(f'"{a}"' if " " in a or "=" in a else a for a in run_args)
        update_cmd += f"{run_parts} {own_image} && docker rm {own_name}_old"

        # Execute: run update in background, then exit
        subprocess.Popen(
            ["sh", "-c", f"sleep 2 && {update_cmd}"],
            start_new_session=True
        )
        sys.exit(0)

    def check_selfupdate_auto(self):
        """Automatic selfupdate check - triggered by scheduler when AUTO_SELFUPDATE=true."""
        hostname = os.environ.get("HOSTNAME", "")
        if not hostname:
            return

        result = subprocess.run(
            ["docker", "inspect", hostname],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return

        config = json.loads(result.stdout)[0]
        own_image = config["Config"]["Image"]
        old_id = config["Image"]
        old_created = config.get("Created", "")[:10]

        # Pull latest silently
        pull = subprocess.run(
            ["docker", "pull", own_image],
            capture_output=True, text=True, timeout=300
        )
        if pull.returncode != 0:
            return

        # Check if image changed
        new_inspect = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}||{{.Created}}", own_image],
            capture_output=True, text=True
        )
        parts = new_inspect.stdout.strip().split("||")
        new_id = parts[0]
        new_created = parts[1][:10] if len(parts) > 1 else "?"

        if new_id == old_id:
            print("Auto selfupdate: already up to date.")
            return

        # Notify and update
        own_name = config["Name"].lstrip("/")
        self.send_message(
            self.t("selfupdate_auto") + "\n"
            + self.t("selfupdate_dates", new=new_created, old=old_created) + "\n"
            + self.t("selfupdate_restarting")
        )

        # Reuse the selfupdate logic
        self._do_selfupdate(config, own_name, own_image)

    def run_updates(self, updater):
        if self.update_running:
            self.send_message(self.t("update_already_running"))
            return

        pending_file = self.config.pending_file
        if not os.path.exists(pending_file):
            self.send_message(self.t("no_pending_updates"))
            return

        with open(pending_file) as f:
            updates = json.load(f)

        if not updates:
            self.send_message(self.t("no_pending_updates"))
            return

        self.update_running = True
        self.send_message(self.t("update_starting", count=len(updates)))

        results = []
        for u in updates:
            try:
                compose_kwargs = {k: u[k] for k in u if k.startswith("compose_")}
                success, msg = updater.update_container(u["name"], u["image"], **compose_kwargs)
                status = "✅" if success else "❌"
                results.append(f"{status} `{u['name']}`: {msg}")
            except Exception as e:
                results.append(f"❌ `{u['name']}`: {str(e)[:200]}")

        try:
            os.remove(pending_file)
        except OSError:
            pass

        self.send_message(self.t("update_result") + "\n\n" + "\n".join(results))
        self.update_running = False

    def listen(self, checker, scheduler):
        import time as _time
        self.start_time = _time.time()

        # Flush old updates from queue to prevent replaying commands after restart
        flush = self.api_call("getUpdates", {"offset": -1, "timeout": 0})
        if flush and flush.get("ok") and flush.get("result"):
            offset = flush["result"][-1]["update_id"] + 1
            print(f"Flushed {len(flush['result'])} old updates from queue.")
        else:
            offset = 0

        print("Bot listener started. Waiting for Telegram messages...")

        while self.running:
            try:
                result = self.api_call("getUpdates", {
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": json.dumps(["callback_query", "message"])
                })

                if not result or not result.get("ok"):
                    import time
                    time.sleep(5)
                    continue

                for update in result.get("result", []):
                    offset = update["update_id"] + 1

                    # Callback buttons
                    callback = update.get("callback_query")
                    if callback:
                        self._handle_callback(callback, checker)
                        continue

                    # Text commands
                    message = update.get("message", {})
                    self._handle_message(message, checker, scheduler)

            except Exception as e:
                print(f"Bot listener error: {e}")
                import time
                time.sleep(5)

        print("Bot listener stopped.")

    def _handle_callback(self, callback, checker):
        data = callback.get("data", "")
        user_id = str(callback["from"]["id"])
        msg_id = callback.get("message", {}).get("message_id")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")

        if user_id != self.config.chat_id:
            self.answer_callback(callback["id"], self.t("not_authorized"))
            return

        if data == "update_all":
            if msg_id and chat_id:
                self.remove_buttons(chat_id, msg_id)
            self.answer_callback(callback["id"], self.t("updates_starting_cb"))
            t = threading.Thread(target=self.run_updates, args=(checker,))
            t.start()
        elif data == "update_skip":
            if msg_id and chat_id:
                self.remove_buttons(chat_id, msg_id)
            self.answer_callback(callback["id"], self.t("ok_manual_cb"))
            self.send_message(self.t("manual_message"))
            try:
                os.remove(self.config.pending_file)
            except OSError:
                pass
        elif data.startswith("update_one:"):
            container_name = data.split(":", 1)[1]
            self.answer_callback(callback["id"], f"Update {container_name}...")
            # Remove only this button, keep the rest
            if msg_id and chat_id:
                self._remove_single_button(chat_id, msg_id, data)
            t = threading.Thread(target=self._run_single_update, args=(checker, container_name))
            t.start()

    def _handle_message(self, message, checker, scheduler):
        text = message.get("text", "")
        user_id = str(message.get("from", {}).get("id", ""))

        if user_id != self.config.chat_id:
            return

        if text == "/status":
            ps = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
                capture_output=True, text=True
            )
            self.send_message(f"{self.t('container_status')}\n```\n{ps.stdout}```")

        elif text == "/check":
            self.send_message(self.t("checking_updates"))
            updates = checker.check_all(bot=self)
            if updates:
                self.notify_updates(updates)
            else:
                self.notify_no_updates()

        elif text == "/updates":
            if os.path.exists(self.config.pending_file):
                with open(self.config.pending_file) as f:
                    pending = json.load(f)
                if pending:
                    names = [f"• `{u['name']}`" for u in pending]
                    self.send_message(self.t("pending_title") + "\n" + "\n".join(names))
                    return
            self.send_message(self.t("no_pending"))

        elif text == "/debug":
            self.config.debug = not self.config.debug
            status = self.t("debug_on") if self.config.debug else self.t("debug_off")
            self.send_message(self.t("debug_mode", status=status))

        elif text == "/cleanup":
            self.send_message(self.t("cleanup_starting"))
            result = subprocess.run(
                ["docker", "image", "prune", "-a", "--force", "--filter", "until=24h"],
                capture_output=True, text=True, timeout=120
            )
            # Extract reclaimed space from output
            lines = result.stdout.strip().split("\n")
            space_line = [l for l in lines if "reclaimed" in l.lower()]
            if space_line:
                self.send_message(f"✅ {space_line[-1]}")
            else:
                self.send_message(self.t("cleanup_none"))

        elif text == "/selfupdate":
            self._handle_selfupdate()

        elif text.startswith("/lang"):
            from i18n import available_languages, get_translator
            langs = available_languages()
            parts = text.split()
            if len(parts) == 2 and parts[1].lower() in langs:
                new_lang = parts[1].lower()
                self.config.language = new_lang
                self.t = get_translator(new_lang)
                self.send_message(self.t("lang_changed"))
            else:
                self.send_message(self.t("lang_usage") + f"\n\n📂 {', '.join(langs)}")

        elif text == "/history":
            if os.path.exists(self.config.history_file):
                with open(self.config.history_file) as f:
                    history = json.load(f)
                if history:
                    # Show last 10 entries, newest first
                    lines = []
                    for h in reversed(history[-10:]):
                        icon = "✅" if h["success"] else "❌"
                        lines.append(f"{icon} `{h['container']}` — {h['timestamp']}\n    {h.get('detail', '')}")
                    self.send_message(self.t("history_title") + "\n\n" + "\n".join(lines))
                    return
            self.send_message(self.t("history_empty"))

        elif text.startswith("/pin"):
            parts = text.split()
            if len(parts) < 2:
                pinned = self._get_pinned()
                if pinned:
                    names = [f"• `{n}`" for n in pinned]
                    self.send_message(self.t("pin_list") + "\n" + "\n".join(names))
                else:
                    self.send_message(self.t("pin_empty"))
                return
            name, err = self._resolve_container(parts[1])
            if err:
                self.send_message(err)
                return
            pinned = self._get_pinned()
            if name not in pinned:
                pinned.append(name)
                self._save_pinned(pinned)
                self.send_message(self.t("pin_added", name=name))
            else:
                self.send_message(self.t("pin_already", name=name))

        elif text.startswith("/unpin"):
            parts = text.split()
            if len(parts) < 2:
                self.send_message(self.t("unpin_usage"))
                return
            # For unpin, match against pinned list too
            partial = parts[1]
            pinned = self._get_pinned()
            matches = [n for n in pinned if n.lower().startswith(partial.lower())]
            if partial in pinned:
                name = partial
            elif len(matches) == 1:
                name = matches[0]
            elif len(matches) > 1:
                self.send_message(self.t("resolve_multiple", names=", ".join(f"`{m}`" for m in matches)))
                return
            else:
                self.send_message(self.t("unpin_not_found", name=partial))
                return
            pinned.remove(name)
            self._save_pinned(pinned)
            self.send_message(self.t("unpin_removed", name=name))

        elif text.startswith("/autoupdate"):
            parts = text.split()
            if len(parts) < 2:
                auto_list = self._get_autoupdate()
                if auto_list:
                    names = [f"• `{n}`" for n in auto_list]
                    self.send_message(self.t("autoupdate_list") + "\n" + "\n".join(names))
                else:
                    self.send_message(self.t("autoupdate_empty"))
                return
            name, err = self._resolve_container(parts[1])
            if err:
                self.send_message(err)
                return
            auto_list = self._get_autoupdate()
            if name in auto_list:
                auto_list.remove(name)
                self._save_autoupdate(auto_list)
                self.send_message(self.t("autoupdate_off", name=name))
            else:
                auto_list.append(name)
                self._save_autoupdate(auto_list)
                self.send_message(self.t("autoupdate_on", name=name))

        elif text == "/settings":
            debug_status = self.t("debug_on") if self.config.debug else self.t("debug_off")
            auto_su = "ON ✅" if self.config.auto_selfupdate else "OFF"
            self.send_message(
                self.t("settings_title") + "\n\n"
                + f"🗓 Schedule: `{self.config.cron_schedule}`\n"
                + f"🌍 {self.t('settings_language')}: `{self.config.language}`\n"
                + f"🔄 Auto-Selfupdate: {auto_su}\n"
                + f"🔍 Debug: {debug_status}\n"
                + f"🚫 Exclude: `{', '.join(self.config.exclude_containers) or '-'}`\n"
                + f"📌 Pinned: `{', '.join(self._get_pinned()) or '-'}`\n"
                + f"⚡ Auto-Update: `{', '.join(self._get_autoupdate()) or '-'}`"
            )

        elif text == "/help" or text == "/start":
            from version import VERSION
            self.send_message(
                self.t("help_title", version=VERSION) + "\n\n"
                + self.t("help_commands") + "\n"
                + self.t("help_status") + "\n"
                + self.t("help_check") + "\n"
                + self.t("help_updates") + "\n"
                + self.t("help_cleanup") + "\n"
                + self.t("help_history") + "\n"
                + self.t("help_pin") + "\n"
                + self.t("help_unpin") + "\n"
                + self.t("help_autoupdate") + "\n"
                + self.t("help_selfupdate") + "\n"
                + self.t("help_debug") + "\n"
                + self.t("help_lang") + "\n"
                + self.t("help_settings") + "\n"
                + self.t("help_help")
            )
