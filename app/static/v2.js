/* Docksentry V2 — renders the status page from JSON.
 *
 * No framework and no build step, on purpose: this project ships a small
 * image with no npm anywhere near it, and drawing a list of containers
 * is not a problem that needs 40 kB of library. What it needs is one
 * render function and a state object, which is what this is.
 *
 * The server sends `/api/v2/status` and nothing else. That is what makes
 * the page a few kilobytes rather than the 173 kB the old one reached at
 * 25 containers, lets a refresh update the list in place, and lets a
 * phone lay a row out differently from a desktop without the server ever
 * knowing which one asked.
 *
 * Every action reuses the v1 endpoints. They already exist, they are
 * already audited, and a second set would be a second thing to keep
 * right — the interface is what is being rebuilt here, not the machinery
 * underneath it.
 */
(function () {
    'use strict';

    var S = {
        rows: [],           // everything the server sent
        hosts: [],
        stats: {},
        can: { update: true, lifecycle: true, settings: true },
        filter: 'all',
        host: '',
        search: '',
        selected: new Set(),
        open: null,         // key of the container whose panel is open
        busy: new Set()
    };

    var T = {
        en: {
            all: 'All', update: 'Updates', pinned: 'Pinned', auto: 'Auto',
            containers: 'containers', updates: 'updates available',
            uptodate: 'up to date', pending: 'update available',
            update: 'Update', check: 'Check', restart: 'Restart',
            stop: 'Stop', start: 'Start', logs: 'Logs', pin: 'Pin',
            unpin: 'Unpin', autoOn: 'Auto-update on', autoOff: 'Auto-update off',
            details: 'Details', none: 'Nothing matches that.',
            selected: 'selected', clear: 'Clear', pinnedNote:
                'Pinned containers are skipped by every update run.',
            autoNote: 'Auto-update applies new images without asking.',
            selfNote: 'Docksentry updates itself through AUTO_SELFUPDATE, ' +
                'not through this list.',
            image: 'Image', host: 'Host', state: 'State', group: 'Group',
            disk: 'disk used',
            note: 'Note', link: 'Link', confirmStop: 'Stop %s?'
        },
        de: {
            all: 'Alle', update: 'Updates', pinned: 'Angepinnt', auto: 'Auto',
            containers: 'Container', updates: 'Updates verfügbar',
            uptodate: 'aktuell', pending: 'Update verfügbar',
            update: 'Aktualisieren', check: 'Prüfen', restart: 'Neu starten',
            stop: 'Stoppen', start: 'Starten', logs: 'Logs', pin: 'Anpinnen',
            unpin: 'Lösen', autoOn: 'Auto-Update an', autoOff: 'Auto-Update aus',
            details: 'Details', none: 'Dazu passt nichts.',
            selected: 'ausgewählt', clear: 'Aufheben', pinnedNote:
                'Angepinnte Container werden bei jedem Update-Lauf übersprungen.',
            autoNote: 'Auto-Update spielt neue Images ohne Rückfrage ein.',
            selfNote: 'Docksentry aktualisiert sich über AUTO_SELFUPDATE, ' +
                'nicht über diese Liste.',
            image: 'Image', host: 'Host', state: 'Zustand', group: 'Gruppe',
            disk: 'Speicher belegt',
            note: 'Notiz', link: 'Link', confirmStop: '%s stoppen?'
        }
    };
    var L = T[(window.DS_V2 && window.DS_V2.lang) === 'de' ? 'de' : 'en'];

    function el(id) { return document.getElementById(id); }
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;',
                     '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    function post(path, fields) {
        var body = Object.keys(fields).map(function (k) {
            return encodeURIComponent(k) + '=' + encodeURIComponent(fields[k]);
        }).join('&');
        return fetch(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: body
        });
    }

    function load() {
        return fetch('/api/v2/status', { headers: { 'Accept': 'application/json' } })
            .then(function (r) {
                if (r.status === 401) { location.href = '/login?next=/'; return null; }
                return r.json();
            })
            .then(function (d) {
                if (!d) return;
                S.rows = d.containers || [];
                S.hosts = d.hosts || [];
                S.stats = d.stats || {};
                S.can = d.can || S.can;
                render();
            })
            .catch(function () { /* a failed refresh keeps the last view */ });
    }

    // ── what is on screen right now ──────────────────────────────────
    function visible() {
        var q = S.search.toLowerCase();
        return S.rows.filter(function (r) {
            if (S.host && r.host !== S.host) return false;
            if (S.filter === 'update' && !r.update) return false;
            if (S.filter === 'pinned' && !r.pinned) return false;
            if (S.filter === 'auto' && !r.auto) return false;
            if (q && (r.name + ' ' + r.image + ' ' + r.group)
                .toLowerCase().indexOf(q) < 0) return false;
            return true;
        });
    }

    function dotClass(r) {
        var h = (r.health || '').toLowerCase();
        if (h === 'healthy') return 'is-healthy';
        if (h === 'unhealthy') return 'is-unhealthy';
        if (h === 'starting') return 'is-starting';
        return 'is-running';
    }

    // ── render ───────────────────────────────────────────────────────
    function render() {
        var rows = visible();

        // The strip. Numbers only, and the one that matters is coloured.
        el('v2-stats').innerHTML =
            stat(S.stats.containers, L.containers) +
            stat(S.stats.updates, L.updates, S.stats.updates > 0) +
            (S.stats.disk == null ? '' :
                stat(S.stats.disk + '%', L.disk + ' · ' + S.stats.disk_free + ' G',
                     S.stats.disk >= 85));

        // Host filter appears only on a multi-host install — a column
        // saying "local" 25 times told nobody anything.
        var hostSel = el('v2-host');
        if (S.hosts.length > 1) {
            if (!hostSel.options.length) {
                hostSel.innerHTML = '<option value="">' + esc(L.host) + ': *</option>' +
                    S.hosts.map(function (h) {
                        return '<option value="' + esc(h.name) + '">' + esc(h.name) + '</option>';
                    }).join('');
                hostSel.hidden = false;
            }
        } else { hostSel.hidden = true; }

        var counts = {
            all: S.rows.length,
            update: S.rows.filter(function (r) { return r.update; }).length,
            pinned: S.rows.filter(function (r) { return r.pinned; }).length,
            auto: S.rows.filter(function (r) { return r.auto; }).length
        };
        Array.prototype.forEach.call(
            document.querySelectorAll('#v2-filters .v2-chip'), function (c) {
                var f = c.dataset.filter;
                c.textContent = L[f] + ' · ' + counts[f];
                c.classList.toggle('is-on', S.filter === f);
                // A filter that would show nothing is not offered.
                c.hidden = f !== 'all' && counts[f] === 0;
            });

        el('v2-list').innerHTML = rows.map(rowHtml).join('');
        el('v2-empty').hidden = rows.length > 0;
        el('v2-empty').textContent = L.none;
        renderBulk();
        if (S.open) renderPanel();
    }

    function stat(n, label, alert) {
        return '<div class="v2-stat' + (alert ? ' is-alert' : '') + '">' +
            '<b>' + esc(n == null ? '—' : n) + '</b><span>' + esc(label) + '</span></div>';
    }

    function rowHtml(r) {
        var sub = r.repo + (r.tag ? ':' + r.tag : '');
        // The button only exists if this caller may press it. A
        // read-only token sees the state and no controls.
        var right = r.update
            ? '<span class="v2-pending">' + esc(L.pending) + '</span>' +
              (S.can.update
                ? '<button type="button" class="v2-btn v2-btn-primary v2-btn-sm" ' +
                  'data-act="update" data-key="' + esc(r.key) + '">' +
                  esc(L.update) + '</button>'
                : '')
            : '<span class="v2-uptodate">' + esc(L.uptodate) + '</span>';
        return '<div class="v2-row' + (S.selected.has(r.key) ? ' is-selected' : '') +
            '" data-key="' + esc(r.key) + '">' +
            '<span class="v2-dot ' + dotClass(r) + '" title="' + esc(r.health || r.state) + '"></span>' +
            '<span class="v2-name"><b>' + esc(r.name) + '</b>' +
            (r.group ? '<span class="v2-tag">' + esc(r.group) + '</span>' : '') +
            (r.pinned ? '<span class="v2-tag">' + esc(L.pinned) + '</span>' : '') +
            (r.auto ? '<span class="v2-tag">' + esc(L.auto) + '</span>' : '') +
            '<span class="v2-sub">' + esc(sub) + '</span></span>' +
            '<span class="v2-update">' + right + '</span>' +
            '<button type="button" class="v2-more" data-act="panel" ' +
            'data-key="' + esc(r.key) + '" aria-label="' + esc(L.details) + '">⋯</button>' +
            '</div>';
    }

    function renderBulk() {
        var bar = el('v2-bulk');
        if (!S.selected.size) { bar.hidden = true; bar.innerHTML = ''; return; }
        bar.hidden = false;
        bar.innerHTML = '<b>' + S.selected.size + ' ' + esc(L.selected) + '</b>' +
            btn('bulk-update', L.update, 'v2-btn-primary') +
            btn('bulk-pin', L.pin) + btn('bulk-unpin', L.unpin) +
            btn('bulk-clear', L.clear);
    }

    function btn(act, label, cls) {
        return '<button type="button" class="v2-btn v2-btn-sm ' + (cls || '') +
            '" data-act="' + act + '">' + esc(label) + '</button>';
    }

    function byKey(key) {
        return S.rows.filter(function (r) { return r.key === key; })[0];
    }

    function renderPanel() {
        var r = byKey(S.open);
        if (!r) { closePanel(); return; }
        var sheet = document.querySelector('#v2-panel .v2-panel-sheet');
        sheet.innerHTML =
            '<button type="button" class="v2-panel-close" data-act="close">×</button>' +
            '<h3>' + esc(r.name) + '</h3>' +
            '<p class="v2-sub">' + esc(r.image) + '</p>' +
            (r.update && S.can.update
                ? '<div class="v2-actions">' +
                  '<button type="button" class="v2-btn v2-btn-primary" data-act="update" ' +
                  'data-key="' + esc(r.key) + '">' + esc(L.update) + '</button></div>'
                : '') +
            sec(L.details,
                '<dl class="v2-kv">' +
                kv(L.state, r.health || r.state) +
                (S.hosts.length > 1 ? kv(L.host, r.host) : '') +
                (r.group ? kv(L.group, r.group) : '') +
                (r.version ? kv('Version', r.version) : '') +
                (r.note ? kv(L.note, r.note) : '') +
                (r.link ? kv(L.link, '<a href="' + esc(r.link) + '" target="_blank" ' +
                    'rel="noopener noreferrer">' + esc(r.link) + '</a>') : '') +
                '</dl>') +
            (!S.can.update ? '' : sec(L.update,
                '<div class="v2-actions">' +
                act('check', L.check, r.key) +
                act(r.pinned ? 'unpin' : 'pin', r.pinned ? L.unpin : L.pin, r.key) +
                act('auto', r.auto ? L.autoOff : L.autoOn, r.key) +
                '</div>' +
                '<p class="v2-hint">' + esc(r.pinned ? L.pinnedNote : L.autoNote) + '</p>' +
                (r.self ? '<p class="v2-hint">' + esc(L.selfNote) + '</p>' : ''))) +
            (r.self || !S.can.lifecycle ? '' : sec(L.state,
                '<div class="v2-actions">' +
                act('restart', L.restart, r.key) +
                '<a class="v2-btn" href="/logs?name=' + encodeURIComponent(r.name) +
                '">' + esc(L.logs) + '</a>' +
                act('stop', L.stop, r.key, 'v2-btn-danger') +
                '</div>'));
    }

    function sec(title, body) {
        return '<section class="v2-sec"><h4>' + esc(title) + '</h4>' + body + '</section>';
    }
    function kv(k, v) { return '<dt>' + esc(k) + '</dt><dd>' + v + '</dd>'; }
    function act(a, label, key, cls) {
        return '<button type="button" class="v2-btn ' + (cls || '') +
            '" data-act="' + a + '" data-key="' + esc(key) + '">' + esc(label) + '</button>';
    }

    function closePanel() { S.open = null; el('v2-panel').hidden = true; }
    function openPanel(key) { S.open = key; el('v2-panel').hidden = false; renderPanel(); }

    // ── actions, all through the endpoints v1 already has ────────────
    var ACTIONS = {
        update:  function (r) { return post('/api/update', { name: r.key }); },
        check:   function (r) { return post('/api/check_one', { name: r.key }); },
        pin:     function (r) { return post('/api/pin', { name: r.key }); },
        unpin:   function (r) { return post('/api/unpin', { name: r.key }); },
        auto:    function (r) { return post('/api/autoupdate', { name: r.key }); },
        restart: function (r) { return post('/api/lifecycle', { name: r.key, action: 'restart' }); },
        stop:    function (r) { return post('/api/lifecycle', { name: r.key, action: 'stop' }); }
    };

    function run(action, key) {
        var r = byKey(key);
        if (!r || S.busy.has(key)) return;
        if (action === 'stop' && !confirm(L.confirmStop.replace('%s', r.name))) return;
        S.busy.add(key);
        ACTIONS[action](r).then(function () {
            S.busy.delete(key);
            return load();
        }).catch(function () { S.busy.delete(key); });
    }

    // ── events ───────────────────────────────────────────────────────
    document.addEventListener('click', function (e) {
        var hit = e.target.closest('[data-act]');
        if (hit) {
            var a = hit.dataset.act, key = hit.dataset.key;
            e.stopPropagation();
            if (a === 'panel') { openPanel(key); return; }
            if (a === 'close') { closePanel(); return; }
            if (a === 'bulk-clear') { S.selected.clear(); render(); return; }
            if (a.indexOf('bulk-') === 0) {
                var what = a.slice(5);
                var keys = Array.from(S.selected);
                Promise.all(keys.map(function (k) {
                    return ACTIONS[what === 'update' ? 'update' : what](byKey(k));
                })).then(function () { S.selected.clear(); load(); });
                return;
            }
            if (ACTIONS[a]) { run(a, key); return; }
        }
        var chip = e.target.closest('.v2-chip');
        if (chip) { S.filter = chip.dataset.filter; render(); return; }
        // Clicking the sheet's backdrop closes it; the sheet itself does not.
        if (e.target.id === 'v2-panel') { closePanel(); return; }
        var row = e.target.closest('.v2-row');
        if (row) {
            // Ctrl/⌘ selects for a bulk action, a plain click opens it —
            // rather than a checkbox column that is empty 99% of the time.
            if (e.ctrlKey || e.metaKey) {
                var k = row.dataset.key;
                if (S.selected.has(k)) S.selected.delete(k); else S.selected.add(k);
                render();
            } else {
                openPanel(row.dataset.key);
            }
        }
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && S.open) closePanel();
        if (e.key === '/' && document.activeElement !== el('v2-search')) {
            e.preventDefault(); el('v2-search').focus();
        }
    });

    function boot() {
        if (!el('v2')) return;
        // The panel moves to <body>. It is position:fixed, but a fixed
        // element inside a transformed or positioned ancestor is confined
        // to that ancestor's stacking context — so no z-index would lift
        // it over the site header, and the theme toggle drew straight
        // across its close button. Two screenshots showed that before the
        // cause was obvious.
        document.body.appendChild(el('v2-panel'));
        el('v2-search').addEventListener('input', function (e) {
            S.search = e.target.value; render();
        });
        el('v2-host').addEventListener('change', function (e) {
            S.host = e.target.value; render();
        });
        el('v2-check').addEventListener('click', function (e) {
            e.target.disabled = true;
            post('/api/check', {}).then(function () {
                e.target.disabled = false; load();
            }).catch(function () { e.target.disabled = false; });
        });
        load();
        // Quiet refresh: the numbers stay true without anyone reloading.
        setInterval(load, 30000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else { boot(); }
})();
