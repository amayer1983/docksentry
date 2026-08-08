(function() {
    // ── Container table: optional Name-column sort (#37, @LeeNX) ──
    // 3-state cycle on the Name header so the deliberate Container-Group
    // order stays the default: group order → A→Z → Z→A → group order.
    var _nameSort = 0, _ctblOrig = null;
    window.sortByName = function() {
        var body = document.getElementById('ctblBody');
        if (!body) return;
        if (_ctblOrig === null) _ctblOrig = Array.prototype.slice.call(body.rows);
        _nameSort = (_nameSort + 1) % 3;
        var arrow = document.getElementById('nameSortArrow');
        var rows;
        if (_nameSort === 0) {
            rows = _ctblOrig.slice();
            if (arrow) arrow.textContent = '';
        } else {
            rows = Array.prototype.slice.call(body.rows);
            rows.sort(function(a, b) {
                var an = (a.querySelector('.container-link') || {}).textContent || '';
                var bn = (b.querySelector('.container-link') || {}).textContent || '';
                return an.localeCompare(bn, undefined, {sensitivity: 'base'});
            });
            if (_nameSort === 2) rows.reverse();
            if (arrow) arrow.textContent = _nameSort === 1 ? ' ▲' : ' ▼';
        }
        rows.forEach(function(r) { body.appendChild(r); });
    };

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
        // A #hash naming a tab wins over the stored one, so links like
        // /settings#updates land where they promise (#51). Otherwise:
        // restore from localStorage, else first tab.
        function known(name) {
            return name && Array.from(buttons).some(function(b){return b.dataset.tabTarget===name;});
        }
        var hash = (location.hash || '').replace(/^#/, '');
        var stored = null;
        try { stored = localStorage.getItem('ds-tab-' + group.dataset.tabs); } catch(e) {}
        var initial = known(hash) ? hash
            : (known(stored) ? stored : (buttons[0] && buttons[0].dataset.tabTarget));
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

// ── Webhook test (Settings page) ──────────────────────────────
// Sends a one-off test message via the current value in the input
// field — uses whatever the user typed even before clicking Save.
// Reports success/failure via a small floating toast.
// ── Test one notification channel (Connections page) ──────────
// Sends through the SAVED settings, not the values currently in the
// form: e-mail alone has seven fields, and a test that only knew the
// one you happened to be standing in would answer about a
// configuration that does not exist anywhere. Save, then test.
function dsTestChannel(name) {
    var btn = event && event.target;
    if (btn) { btn.disabled = true; btn.dataset.origText = btn.textContent; btn.textContent = '…'; }
    fetch('/api/test_channel', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: 'name=' + encodeURIComponent(name),
    }).then(function(r) {
        return r.json().catch(function() { return {ok: false, error: 'HTTP ' + r.status}; });
    }).then(function(data) {
        if (data.ok && data.note) {
            dsToast('Sent \u2713 — ' + data.note, 'warn');
        } else if (data.ok) {
            dsToast('Test message sent \u2713', 'success');
        } else {
            dsToast('Failed: ' + (data.error || 'unknown'), 'danger');
        }
    }).catch(function(e) {
        dsToast('Network error: ' + e.message, 'danger');
    }).finally(function() {
        if (btn) { btn.disabled = false; btn.textContent = btn.dataset.origText || 'Send test'; }
    });
}

// ── Per-container update check (Status page, #50) ─────────────
// The global check button fires a thread and redirects straight back
// to a page still showing the old numbers; the only feedback is a
// Telegram/webhook notification, which people running neither never
// see. This one waits for the answer (a single registry HEAD) and
// says so on screen. The button stays disabled for the round trip —
// that's both the double-click guard and what keeps an impatient
// user from hammering the registry into a rate limit.
// All wording comes from data-* attributes: the backend owns the
// translations, this file has no access to them.
function dsCheckOne(btn) {
    if (!btn || btn.disabled) return;
    var name = btn.dataset.name || '';
    if (!name) return;
    function release() {
        btn.disabled = false;
        btn.style.opacity = '';
    }
    btn.disabled = true;
    btn.style.opacity = '0.5';
    fetch('/api/check_one', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: 'name=' + encodeURIComponent(name),
    }).then(function(r) {
        return r.json().catch(function() { return {ok: false, error: 'HTTP ' + r.status}; });
    }).then(function(data) {
        // Only present when DEBUG is on. Console, not the page: it can be
        // a few dozen lines and it names registries and repositories.
        if (data.debug && data.debug.length) {
            try { console.log('[docksentry] check ' + name + '\n' + data.debug.join('\n')); } catch (e) {}
        }
        if (!data.ok) {
            if (data.busy) {
                dsToast(btn.dataset.msgBusy || 'An update is already running', 'warn');
            } else {
                var msg = btn.dataset.msgError || 'Check failed';
                if (data.error) msg += ' (' + data.error + ')';
                // 'danger', not 'error': the toast kind becomes the CSS class
                // `is-<kind>`, and app.css only defines is-success / is-warn /
                // is-danger. 'error' silently renders without the red bar.
                dsToast(msg, 'danger');
            }
            release();
            return;
        }
        if (data.found) {
            // Reload so the pending badge, the counter and the Update
            // button actually show up — they're rendered server-side.
            dsToast(btn.dataset.msgFound || 'Update available', 'success');
            setTimeout(function() { window.location.reload(); }, 1200);
            return;
        }
        dsToast(btn.dataset.msgNone || 'Up to date');
        release();
    }).catch(function(e) {
        dsToast((btn.dataset.msgError || 'Check failed') + ' (' + e.message + ')', 'danger');
        release();
    });
}

// ── Cron schedule live preview (Settings page) ────────────────
// Debounced (300ms) so we don't spam /api/cron_preview while the
// user is mid-typing. Backend returns next 3 ticks for the
// expression — see scheduler.cron_next_ticks.
var _dsCronTimer = null;
function dsCronPreview() {
    var input = document.getElementById('f-cron_schedule');
    var preview = document.getElementById('cron-preview');
    if (!input || !preview) return;
    var expr = input.value.trim();
    clearTimeout(_dsCronTimer);
    _dsCronTimer = setTimeout(function() {
        fetch('/api/cron_preview?expr=' + encodeURIComponent(expr))
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.ok) {
                    if (data.ticks && data.ticks.length) {
                        preview.innerHTML = '⏰ ' + data.ticks.join(' · ');
                        preview.style.color = 'var(--muted)';
                    } else {
                        preview.textContent = '⚠ No tick in the next year';
                        preview.style.color = 'var(--warn, #d29922)';
                    }
                } else {
                    preview.textContent = '⚠ ' + (data.error || 'Invalid cron expression');
                    preview.style.color = 'var(--warn, #d29922)';
                }
            })
            .catch(function(e) {
                preview.textContent = '⚠ ' + e.message;
                preview.style.color = 'var(--warn, #d29922)';
            });
    }, 300);
}
// Trigger initial preview on page load if the field exists.
document.addEventListener('DOMContentLoaded', function() {
    if (document.getElementById('f-cron_schedule')) {
        dsCronPreview();
    }
});

// ── Container Groups drag-and-drop (Groups page) ─────────────
// HTML5 native drag-drop on .group-member elements. On drop, post
// the new order to /api/group_reorder_batch and toast the result.
// Falls back gracefully — if JS is off the existing ↑/↓ form buttons
// on the legacy Settings tab still work (kept for users who haven't
// migrated to the dedicated page).
(function dsInitGroupDrag() {
    document.querySelectorAll('.group-members-list').forEach(function(list) {
        var dragged = null;
        list.querySelectorAll('.group-member').forEach(function(item) {
            item.addEventListener('dragstart', function(e) {
                dragged = item;
                item.classList.add('dragging');
                try { e.dataTransfer.effectAllowed = 'move'; } catch (err) {}
            });
            item.addEventListener('dragend', function() {
                item.classList.remove('dragging');
                dragged = null;
            });
            item.addEventListener('dragover', function(e) {
                e.preventDefault();
                if (!dragged || dragged === item) return;
                var rect = item.getBoundingClientRect();
                var after = (e.clientY - rect.top) > rect.height / 2;
                if (after) {
                    item.parentNode.insertBefore(dragged, item.nextSibling);
                } else {
                    item.parentNode.insertBefore(dragged, item);
                }
            });
            item.addEventListener('drop', function(e) {
                e.preventDefault();
                dsPersistGroupOrder(list);
            });
        });
    });
})();

function dsPersistGroupOrder(list) {
    var gid = list.dataset.groupId;
    var order = Array.from(list.querySelectorAll('.group-member'))
                     .map(function(li) { return li.dataset.container; })
                     .filter(Boolean);
    if (!gid || !order.length) return;
    var body = 'group_id=' + encodeURIComponent(gid);
    order.forEach(function(c) { body += '&containers=' + encodeURIComponent(c); });
    fetch('/api/group_reorder_batch', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: body,
    }).then(function(r) {
        return r.json().catch(function() { return {ok: false}; });
    }).then(function(data) {
        if (data.ok) {
            dsToast('Order saved ✓', 'success');
            // Refresh head badge: only the first member should carry it.
            list.querySelectorAll('.group-member').forEach(function(li, idx) {
                var badges = li.querySelectorAll('.badge.badge-purple');
                badges.forEach(function(b) { b.remove(); });
            });
            var first = list.querySelector('.group-member');
            if (first && first.querySelector('code')) {
                var b = document.createElement('span');
                b.className = 'badge badge-purple';
                b.textContent = 'HEAD';
                first.querySelector('code').after(document.createTextNode(' '));
                first.querySelector('code').after(b);
            }
        } else {
            dsToast('Reorder failed — refreshing', 'danger');
            setTimeout(function() { location.reload(); }, 1500);
        }
    }).catch(function(e) {
        dsToast('Network error: ' + e.message, 'danger');
    });
}

// ── Backup import (Settings page) ───────────────────────────
// POSTs the selected file (a JSON bundle from /api/backup_export)
// to /api/backup_import. Confirms with the user first since this
// overwrites live state. Reloads on success so the new state
// renders in every view.
function dsBackupImport(input) {
    var file = input.files && input.files[0];
    if (!file) return;
    if (!confirm('Restore from "' + file.name + '"? This will overwrite groups, notes, links, pins, autoupdate flags, update windows, and persisted settings.')) {
        input.value = '';
        return;
    }
    var fd = new FormData();
    fd.append('file', file);
    fetch('/api/backup_import', { method: 'POST', body: fd })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.ok) {
                var keys = (data.restored || []).join(', ') || 'nothing';
                dsToast('Restored: ' + keys, 'success');
                setTimeout(function() { location.reload(); }, 1200);
            } else {
                dsToast('Import failed: ' + (data.error || 'unknown'), 'danger');
                input.value = '';
            }
        })
        .catch(function(e) {
            dsToast('Network error: ' + e.message, 'danger');
            input.value = '';
        });
}

// ── Container Groups: auto-detect from Compose/Portainer (Groups page) ──
// Click "🔍 Auto-detect" → fetch /api/groups_detect → render a modal with
// per-stack cards. User checks which stacks to import + optionally tweaks
// order (drag-drop) and restart_dependents per card, then "Import selected"
// POSTs to /api/groups_import_batch and reloads.
function dsAutoDetectGroups() {
    var modal = document.getElementById('autodetect-modal');
    var body = document.getElementById('autodetect-body');
    if (!modal || !body) return;
    body.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:24px">Scanning containers…</div>';
    modal.classList.add('is-open');
    fetch('/api/groups_detect')
        .then(function(r) { return r.json(); })
        .then(function(data) { dsAutoDetectRender(data); })
        .catch(function(e) {
            body.innerHTML = '<div style="color:var(--warn,#d29922);text-align:center;padding:24px">⚠ ' + e.message + '</div>';
        });
}
function dsAutoDetectClose() {
    var modal = document.getElementById('autodetect-modal');
    if (modal) modal.classList.remove('is-open');
}
function dsAutoDetectRender(data) {
    var body = document.getElementById('autodetect-body');
    if (!body) return;
    if (!data.ok || !data.stacks || !data.stacks.length) {
        body.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:24px">📦<br>No Compose / Portainer / Swarm stacks detected.<br><span style="font-size:11px">Stacks need com.docker.compose.project or com.docker.stack.namespace labels.</span></div>';
        return;
    }
    var html = '';
    data.stacks.forEach(function(stack, sIdx) {
        var memberCount = stack.containers.length;
        var isMulti = memberCount > 1;
        var hasNetns = stack.containers.some(function(c) { return !!c.netns_hint; });

        // Default-check policy (v1.23.4): NOTHING is checked by default.
        // "Import selected" must mean "import what I selected" — opening
        // the modal to look around and clicking Import should import
        // nothing, not silently create groups for whatever was pre-
        // checked. Reported by @famewolf in #2: he opened auto-detect to
        // browse, checked nothing, clicked Import, and a multi-container
        // stack (which v1.21.2 pre-checked) got imported anyway.
        // Already-imported stacks stay disabled. The "single" badge below
        // still steers users away from single-container stacks.
        var importChecked = '';
        var importDisabled = stack.exists ? ' disabled' : '';

        // restart_dependents control (group-level, moved to header in v1.21.2):
        //   - Disabled + grayed out for single-container stacks (no effect)
        //   - Pre-checked when we detected netns sharing (VPN-sidecar pattern)
        //   - Hint badge appears when recommended
        var rdDisabled = !isMulti ? ' disabled' : '';
        var rdChecked = (isMulti && hasNetns) ? ' checked' : '';
        var rdHint = hasNetns
            ? '<span class="badge badge-purple" title="At least one container shares a network namespace (container:*) — recommended for VPN-sidecar stacks.">netns recommended</span>'
            : '';

        var existsBadge = stack.exists ? ' <span class="badge badge-yellow" title="Same-named group already exists — re-importing will update it in place.">already imported</span>' : '';
        var sourceBadge = '<span class="badge badge-blue">' + (stack.source || 'unknown') + '</span>';
        var singleHint = isMulti ? '' : ' <span class="badge badge-yellow" title="Single-container stack — making it a group has no effect because there are no dependents to coordinate.">single</span>';

        var memberRows = '';
        stack.containers.forEach(function(c, idx) {
            var conflict = stack.conflicts && stack.conflicts[c.name];
            var conflictBadge = conflict ? ' <span class="badge badge-yellow" title="Already in another Docksentry group — will be moved on import.">↻ ' + dsEscape(conflict) + '</span>' : '';
            var netnsBadge = c.netns_hint ? ' <span class="badge badge-purple" title="NetworkMode=' + dsEscape(c.netns_hint) + '">netns</span>' : '';
            var headBadge = idx === 0 && isMulti ? ' <span class="badge badge-purple">HEAD</span>' : '';
            memberRows +=
                '<li class="ad-member" draggable="true" data-container="' + dsEscape(c.name) + '" data-stack="' + sIdx + '">' +
                    '<span class="drag-handle">⠿</span>' +
                    '<label style="flex:1;cursor:pointer">' +
                        '<input type="checkbox" class="ad-include" checked> ' +
                        '<code>' + dsEscape(c.name) + '</code>' +
                        (c.service ? ' <span style="color:var(--text-muted);font-size:11px">→ ' + dsEscape(c.service) + '</span>' : '') +
                        headBadge + netnsBadge + conflictBadge +
                    '</label>' +
                '</li>';
        });

        html +=
            '<div class="ad-stack" data-stack-idx="' + sIdx + '" data-stack-name="' + dsEscape(stack.name) + '" style="border:1px solid var(--border);border-radius:6px;padding:12px;margin-bottom:12px;' + (stack.exists ? 'opacity:0.55' : '') + '">' +
                // ── Header: include-stack checkbox + name + badges + RD toggle ──
                '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">' +
                    '<input type="checkbox" class="ad-stack-import"' + importChecked + importDisabled + '>' +
                    '<strong>' + dsEscape(stack.name) + '</strong> ' + sourceBadge + singleHint + existsBadge +
                    '<span style="margin-left:auto;color:var(--text-muted);font-size:11px">' + memberCount + ' container' + (memberCount === 1 ? '' : 's') + '</span>' +
                '</div>' +
                // ── RD toggle inline under the header (group-level option) ──
                '<label class="ad-rd-label" style="display:flex;align-items:center;gap:6px;font-size:12px;margin:4px 0 8px 0;color:' + (isMulti ? 'var(--text)' : 'var(--text-muted)') + '">' +
                    '<input type="checkbox" class="ad-rd"' + rdChecked + rdDisabled + '>' +
                    '🔁 <code style="font-size:11px">restart_dependents</code>' +
                    (rdHint ? ' ' + rdHint : '') +
                    (!isMulti ? ' <span style="color:var(--text-muted);font-size:11px">(no effect — single container)</span>' : '') +
                '</label>' +
                // ── Members list with drag-drop reorder ──
                '<ul class="ad-members" style="list-style:none;padding:0;margin:6px 0 0 0">' + memberRows + '</ul>' +
            '</div>';
    });
    body.innerHTML = html;
    dsAutoDetectBindDrag();
}
function dsAutoDetectBindDrag() {
    document.querySelectorAll('#autodetect-body .ad-members').forEach(function(list) {
        var dragged = null;
        list.querySelectorAll('.ad-member').forEach(function(item) {
            item.addEventListener('dragstart', function() {
                dragged = item; item.classList.add('dragging');
            });
            item.addEventListener('dragend', function() {
                item.classList.remove('dragging'); dragged = null;
                dsAutoDetectRefreshHeadBadges(list);
            });
            item.addEventListener('dragover', function(e) {
                e.preventDefault();
                if (!dragged || dragged === item) return;
                var rect = item.getBoundingClientRect();
                var after = (e.clientY - rect.top) > rect.height / 2;
                if (after) item.parentNode.insertBefore(dragged, item.nextSibling);
                else item.parentNode.insertBefore(dragged, item);
            });
        });
    });
}
function dsAutoDetectRefreshHeadBadges(list) {
    list.querySelectorAll('.ad-member').forEach(function(li, idx) {
        var label = li.querySelector('label');
        // Strip existing head badge, re-add to first.
        var existingHead = li.querySelector('.badge.badge-purple');
        if (existingHead && existingHead.textContent === 'HEAD') existingHead.remove();
        if (idx === 0) {
            var b = document.createElement('span');
            b.className = 'badge badge-purple';
            b.textContent = 'HEAD';
            label.appendChild(document.createTextNode(' '));
            label.appendChild(b);
        }
    });
}
function dsAutoDetectImport() {
    var btn = document.getElementById('autodetect-import');
    if (btn) { btn.disabled = true; btn.textContent = '…'; }
    var stacks = [];
    document.querySelectorAll('#autodetect-body .ad-stack').forEach(function(card) {
        var importCb = card.querySelector('.ad-stack-import');
        if (!importCb || !importCb.checked || importCb.disabled) return;
        var sname = card.dataset.stackName;
        var containers = [];
        card.querySelectorAll('.ad-member').forEach(function(li) {
            var inc = li.querySelector('.ad-include');
            if (inc && inc.checked) containers.push(li.dataset.container);
        });
        if (!containers.length) return;
        var rd = card.querySelector('.ad-rd').checked;
        stacks.push({
            name: sname,
            containers: containers,
            restart_dependents: rd,
            wait_seconds: 30,
        });
    });
    if (!stacks.length) {
        dsToast('Nothing selected', 'warn');
        if (btn) { btn.disabled = false; btn.textContent = 'Import selected'; }
        return;
    }
    var body = '';
    stacks.forEach(function(s) {
        body += (body ? '&' : '') + 'stacks=' + encodeURIComponent(JSON.stringify(s));
    });
    fetch('/api/groups_import_batch', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: body,
    }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.ok) {
            dsToast(data.created + ' stack(s) imported ✓', 'success');
            setTimeout(function() { location.reload(); }, 700);
        } else {
            dsToast('Import failed', 'danger');
            if (btn) { btn.disabled = false; btn.textContent = 'Import selected'; }
        }
    }).catch(function(e) {
        dsToast('Network error: ' + e.message, 'danger');
        if (btn) { btn.disabled = false; btn.textContent = 'Import selected'; }
    });
}
function dsEscape(s) {
    var d = document.createElement('div');
    d.textContent = s; return d.innerHTML;
}
