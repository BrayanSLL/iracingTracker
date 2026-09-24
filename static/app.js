'use strict';

const $ = id => document.getElementById(id);
const cssVar = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const state = {
    status: null,
    sessionId: null,     // session affichée
    follow: true,        // suivre automatiquement la session en cours d'enregistrement
    detail: null,
    detailKey: null,
    sessionsKey: null,
    lapId: null,         // tour analysé
    refId: null,         // tour de référence
    lapPinned: false,    // choisi à la main (sinon : dernier tour)
    refPinned: false,    // choisi à la main (sinon : meilleur tour)
    compareKey: null,
};
let lapTimeChart = null;
let teleCharts = [];
let syncingZoom = false;

// --- formatage ----------------------------------------------------------------

function fmtTime(s) {
    if (s == null) return '—';
    const ms = Math.round(s * 1000);
    const m = Math.floor(ms / 60000);
    return `${m}:${((ms % 60000) / 1000).toFixed(3).padStart(6, '0')}`;
}
function fmtDelta(d, digits = 3) {
    if (d == null) return '—';
    const sign = d > 0 ? '+' : d < 0 ? '−' : '±';
    return sign + Math.abs(d).toFixed(digits);
}
const fmtNum = (v, digits, unit = '') => v == null ? '—' : `${v.toFixed(digits)}${unit}`;
const fmtPct = v => v == null ? '—' : `${Math.round(v * 100)} %`;
const escapeHtml = s => String(s ?? '').replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
const FLAGS = { pit: 'stand', partial: 'partiel' };

async function api(url, options) {
    const res = await fetch(url, options);
    if (!res.ok) {
        let message = `${res.status}`;
        try { message = (await res.json()).error || message; } catch (_) { /* pas de JSON */ }
        throw new Error(message);
    }
    return res.status === 204 ? null : res.json();
}

// --- statut en direct ---------------------------------------------------------

async function pollStatus() {
    try {
        const s = await api('/api/status');
        state.status = s;
        const pill = $('status');
        pill.className = 'status-pill' + (s.connected ? (s.on_track ? ' on-track' : ' connected') : '');
        $('status-text').textContent = !s.connected ? 'Hors ligne' : s.on_track ? 'En piste' : 'Connecté';
        $('live-session').textContent = s.connected ? [s.track, s.car].filter(Boolean).join(' · ') : '—';
        $('live').hidden = !(s.connected && s.on_track);
        if (s.connected) {
            $('live-gear').textContent = s.gear === -1 ? 'R' : s.gear === 0 ? 'N' : (s.gear ?? '—');
            $('live-speed').textContent = fmtNum(s.speed_kmh, 0, ' km/h');
            $('live-laptime').textContent = fmtTime(s.lap_time);
            $('live-throttle').style.width = `${Math.round((s.throttle || 0) * 100)}%`;
            $('live-brake').style.width = `${Math.round((s.brake || 0) * 100)}%`;
        }
        if (state.follow && s.session_id && s.session_id !== state.sessionId) {
            selectSession(s.session_id, true);
        }
    } catch (err) {
        $('status').className = 'status-pill';
        $('status-text').textContent = 'Serveur injoignable';
    }
}

// --- liste des sessions -------------------------------------------------------

async function refreshSessions() {
    const sessions = await api('/api/sessions');
    const liveId = state.status && state.status.connected ? state.status.session_id : null;
    const key = JSON.stringify([sessions, liveId, state.sessionId]);
    if (key === state.sessionsKey) return;
    state.sessionsKey = key;

    $('sessions-empty').hidden = sessions.length > 0;
    $('session-list').innerHTML = sessions.map(s => `
        <li class="session-item${s.id === state.sessionId ? ' selected' : ''}" data-id="${s.id}">
            <div>
                <div class="name">${escapeHtml(s.name || s.track || 'Circuit inconnu')}${s.id === liveId ? '<span class="live-tag">● en cours</span>' : ''}</div>
                <div class="meta">${escapeHtml([s.name ? s.track : null, s.car].filter(Boolean).join(' · '))}</div>
                <div class="meta">${s.started_at.replace('T', ' ').slice(0, 16)} · ${escapeHtml(s.session_type || '')}</div>
                <div class="meta">${s.laps} tours · meilleur ${fmtTime(s.best_lap)}</div>
                ${s.note ? `<div class="note-preview">${escapeHtml(s.note.length > 60 ? s.note.slice(0, 60) + '…' : s.note)}</div>` : ''}
            </div>
            <button class="icon-btn" type="button" data-delete="${s.id}" title="Supprimer la session" aria-label="Supprimer la session">✕</button>
        </li>`).join('');
}

$('session-list').addEventListener('click', event => {
    const del = event.target.closest('[data-delete]');
    if (del) {
        deleteSession(Number(del.dataset.delete));
        return;
    }
    const item = event.target.closest('.session-item');
    if (item) {
        const id = Number(item.dataset.id);
        selectSession(id, !!(state.status && state.status.session_id === id));
    }
});

async function deleteSession(id) {
    if (!confirm(`Supprimer définitivement la session #${id} et toute sa télémétrie ?`)) return;
    try {
        await api(`/api/sessions/${id}`, { method: 'DELETE' });
    } catch (err) {
        alert(err.message);
        return;
    }
    if (state.sessionId === id) {
        state.sessionId = null;
        state.follow = true;
        showSession(null);
    }
    state.sessionsKey = null;
    await refreshSessions();
}

$('delete-btn').addEventListener('click', () => state.sessionId && deleteSession(state.sessionId));

// --- session affichée ---------------------------------------------------------

function selectSession(id, follow) {
    state.sessionId = id;
    state.follow = follow;
    state.detailKey = null;
    state.compareKey = null;
    state.lapId = state.refId = null;
    state.lapPinned = state.refPinned = false;
    state.sessionsKey = null;
    refreshSessions().catch(console.error);
    refreshDetail().catch(console.error);
}

function showSession(visible) {
    $('session-view').hidden = !visible;
    $('no-session').hidden = !!visible;
}

async function refreshDetail() {
    const id = state.sessionId;
    if (!id) { showSession(false); return; }
    let detail;
    try {
        detail = await api(`/api/sessions/${id}`);
    } catch (err) {
        state.sessionId = null;
        showSession(false);
        return;
    }
    if (id !== state.sessionId) return;  // l'utilisateur a changé de session entre-temps
    const last = detail.laps[detail.laps.length - 1];
    const key = `${id}:${detail.laps.length}:${last ? last.id : ''}`;
    if (key === state.detailKey) return;
    state.detailKey = key;
    state.detail = detail;
    renderDetail(detail);
}

function renderDetail({ session, laps, stats, record }) {
    showSession(true);
    if (document.activeElement !== $('session-name')) $('session-name').value = session.name || session.track || '';
    if (document.activeElement !== $('session-note')) $('session-note').value = session.note || '';
    $('session-subtitle').textContent = [session.name ? session.track : null, session.car, session.session_type,
        session.started_at.replace('T', ' ').slice(0, 16)].filter(Boolean).join(' · ');
    $('export-btn').href = `/api/sessions/${session.id}/export.csv`;

    $('stat-best').textContent = fmtTime(stats.best_lap);
    $('stat-avg').textContent = fmtTime(stats.avg_lap);
    $('stat-stdev').textContent = stats.stdev == null ? '—' : `± ${stats.stdev.toFixed(3)} s`;
    $('stat-ideal').textContent = fmtTime(stats.ideal_lap);
    $('stat-ideal-note').textContent = stats.ideal_lap != null && stats.best_lap != null
        ? `${fmtDelta(stats.ideal_lap - stats.best_lap)} s vs meilleur tour` : '';
    $('stat-laps').textContent = `${stats.clean_laps} / ${stats.laps}`;
    $('stat-fuel').textContent = fmtNum(stats.avg_fuel, 2, ' L');

    // choix par défaut : dernier tour enregistré vs meilleur tour
    const withTrace = laps.filter(l => l.has_trace);
    if (!state.lapPinned || !laps.some(l => l.id === state.lapId)) {
        state.lapId = withTrace.length ? withTrace[withTrace.length - 1].id : null;
    }
    if (!state.refPinned || !isKnownLap(state.refId)) {
        state.refId = defaultReference(laps, stats);
    }

    loadDebrief(session.id);
    renderLapTimeChart(laps, stats);
    renderLapTable(laps, stats);
    renderSelectors(laps);
    loadCompare();
}

function isKnownLap(id) {
    const d = state.detail;
    return !!d && (d.laps.some(l => l.id === id) || (d.record && d.record.id === id));
}

function defaultReference(laps, stats) {
    // par défaut : ton record toutes sessions confondues sur ce couple voiture × circuit
    const record = state.detail && state.detail.record;
    if (record && record.id !== state.lapId) return record.id;
    if (stats.best_lap_id && stats.best_lap_id !== state.lapId) return stats.best_lap_id;
    // le tour analysé est le meilleur : on le compare au 2e meilleur tour propre
    const others = laps.filter(l => !l.flag && l.lap_time != null && l.has_trace && l.id !== state.lapId)
        .sort((a, b) => a.lap_time - b.lap_time);
    return others.length ? others[0].id : null;
}

function renderLapTable(laps, stats) {
    $('laps-empty').hidden = laps.length > 0;
    $('laps-body').innerHTML = laps.slice().reverse().map(l => {
        const isBest = l.id === stats.best_lap_id;
        const delta = l.lap_time != null && stats.best_lap != null && !isBest ? fmtDelta(l.lap_time - stats.best_lap) : '';
        const classes = [l.id === state.lapId ? 'analysed' : '', l.id === state.refId ? 'reference' : ''].join(' ');
        return `<tr class="${classes}" data-lap="${l.id}">
            <td>${l.lap_number}</td>
            <td class="${isBest ? 'best' : ''}">${fmtTime(l.lap_time)}</td>
            <td>${isBest ? '<span class="best">meilleur</span>' : delta}</td>
            <td>${fmtNum(l.fuel_used, 2, ' L')}</td>
            <td>${fmtNum(l.max_speed_kmh, 0, ' km/h')}</td>
            <td>${fmtPct(l.throttle_avg)}</td>
            <td>${fmtPct(l.brake_avg)}</td>
            <td class="note">${FLAGS[l.flag] || ''}</td>
            <td>${l.has_trace ? `<button class="btn ref-btn" type="button" data-ref="${l.id}" title="Utiliser comme référence">Réf.</button>` : ''}${state.detail && state.detail.is_race ? `<button class="btn lap-del" type="button" data-del-lap="${l.id}" title="Supprimer ce tour (trafic, incident…)" aria-label="Supprimer le tour ${l.lap_number}">✕</button>` : ''}</td>
        </tr>`;
    }).join('');
}

$('laps-body').addEventListener('click', async event => {
    const delBtn = event.target.closest('[data-del-lap]');
    if (delBtn) {
        const lap = state.detail.laps.find(l => l.id === Number(delBtn.dataset.delLap));
        if (!confirm(`Supprimer le tour ${lap.lap_number} (${fmtTime(lap.lap_time)}) ? Il ne comptera plus dans les stats.`)) return;
        try {
            await api(`/api/laps/${lap.id}`, { method: 'DELETE' });
        } catch (err) {
            alert(err.message);
            return;
        }
        state.detailKey = null;
        state.sessionsKey = null;
        await refreshDetail();
        refreshSessions().catch(console.error);
        return;
    }
    const refBtn = event.target.closest('[data-ref]');
    if (refBtn) {
        state.refId = Number(refBtn.dataset.ref);
        state.refPinned = true;
    } else {
        const row = event.target.closest('tr[data-lap]');
        if (!row) return;
        state.lapId = Number(row.dataset.lap);
        state.lapPinned = true;
        if (!state.refPinned) state.refId = defaultReference(state.detail.laps, state.detail.stats);
    }
    renderLapTable(state.detail.laps, state.detail.stats);
    renderSelectors(state.detail.laps);
    loadCompare();
});

function lapLabel(l) {
    return `Tour ${l.lap_number} · ${fmtTime(l.lap_time)}${l.flag ? ` (${FLAGS[l.flag]})` : ''}`;
}

function renderSelectors(laps) {
    const options = laps.filter(l => l.has_trace).slice().reverse();
    $('lap-select').innerHTML = options.map(l =>
        `<option value="${l.id}"${l.id === state.lapId ? ' selected' : ''}>${lapLabel(l)}</option>`).join('');
    const record = state.detail && state.detail.record;
    const recordOption = record && !laps.some(l => l.id === record.id)
        ? `<option value="${record.id}"${record.id === state.refId ? ' selected' : ''}>🏆 Record · ${fmtTime(record.lap_time)} (${record.started_at.slice(0, 10)})</option>` : '';
    $('ref-select').innerHTML = '<option value="">Aucune</option>' + recordOption + options.map(l =>
        `<option value="${l.id}"${l.id === state.refId ? ' selected' : ''}>${record && record.id === l.id ? '🏆 ' : ''}${lapLabel(l)}</option>`).join('');
}

$('lap-select').addEventListener('change', e => {
    state.lapId = Number(e.target.value);
    state.lapPinned = true;
    renderLapTable(state.detail.laps, state.detail.stats);
    loadCompare();
});
$('ref-select').addEventListener('change', e => {
    state.refId = e.target.value ? Number(e.target.value) : null;
    state.refPinned = true;
    renderLapTable(state.detail.laps, state.detail.stats);
    loadCompare();
});

// --- graphique des temps au tour ----------------------------------------------

function baseAxis(extra) {
    return Object.assign({
        stroke: cssVar('--text-muted'),
        grid: { stroke: cssVar('--grid'), width: 1 },
        ticks: { stroke: cssVar('--grid'), width: 1 },
        font: '12px -apple-system, "Segoe UI", sans-serif',
    }, extra);
}

function padRange(padding) {
    return (u, min, max) => min === max ? [min - padding, max + padding] : [min - (max - min) * 0.1, max + (max - min) * 0.1];
}

function renderLapTimeChart(laps, stats) {
    const clean = laps.filter(l => !l.flag && l.lap_time != null);
    const container = $('laptime-chart');
    if (lapTimeChart) { lapTimeChart.destroy(); lapTimeChart = null; }
    $('laptime-empty').hidden = clean.length > 0;
    if (!clean.length) return;

    const x = clean.map(l => l.lap_number);
    const data = [x, clean.map(l => l.lap_time), clean.map(() => stats.avg_lap)];
    lapTimeChart = new uPlot({
        width: container.clientWidth,
        height: 220,
        scales: { x: { time: false, range: padRange(1) }, y: { range: padRange(0.5) } },
        cursor: { drag: { x: false, y: false }, points: { size: 10 } },
        axes: [
            baseAxis({ label: 'Tour', labelSize: 20, incrs: [1, 2, 5, 10, 20, 50],
                       values: (u, splits) => splits.map(v => Number.isInteger(v) ? v : '') }),
            baseAxis({ size: 70, values: (u, splits) => splits.map(v => fmtTime(v)) }),
        ],
        series: [
            { label: 'Tour', value: (u, v) => v == null ? '—' : v },
            { label: 'Temps', stroke: cssVar('--series-1'), width: 2,
              points: { show: true, size: 8, fill: cssVar('--series-1'), stroke: cssVar('--surface-1'), width: 2 },
              value: (u, v) => fmtTime(v) },
            { label: 'Moyenne', stroke: cssVar('--text-muted'), width: 1.5, dash: [6, 4],
              points: { show: false }, value: (u, v) => fmtTime(v) },
        ],
    }, data, container);
}

// --- télémétrie ---------------------------------------------------------------

async function loadCompare() {
    const key = `${state.lapId}-${state.refId}`;
    if (key === state.compareKey) return;
    state.compareKey = key;
    renderSectors(null);
    state.compare = null;
    if (!state.lapId) {
        destroyTeleCharts();
        renderAnalysis(null);
        showTeleMessage('Aucun tour avec télémétrie dans cette session.');
        return;
    }
    let data;
    try {
        const refParam = state.refId ? `&ref=${state.refId}` : '';
        data = await api(`/api/compare?lap=${state.lapId}${refParam}`);
    } catch (err) {
        destroyTeleCharts();
        renderAnalysis(null);
        showTeleMessage(err.message);
        return;
    }
    if (key !== state.compareKey) return;  // sélection changée pendant le chargement
    showTeleMessage(null);
    state.compare = data;
    renderSectors(data);
    renderTeleCharts(data);
    renderAnalysis(data);
}

function showTeleMessage(message) {
    $('tele-empty').hidden = !message;
    $('tele-empty').textContent = message || '';
}

function renderSectors(data) {
    const lap = data && data.lap_meta;
    const ref = data && data.ref_meta;
    if (!lap || !lap.sectors) { $('sectors').innerHTML = ''; return; }
    $('sectors').innerHTML = lap.sectors.map((t, i) => {
        const d = ref && ref.sectors ? t - ref.sectors[i] : null;
        const tone = d == null ? 0 : Math.min(100, Math.round(Math.abs(d) / 0.25 * 100));
        const color = d == null || Math.abs(d) < 0.0005 ? 'var(--neutral)'
            : `color-mix(in srgb, ${d > 0 ? 'var(--loss)' : 'var(--gain)'} ${Math.max(tone, 25)}%, var(--neutral))`;
        return `<div class="sector" style="background:${color}" title="Secteur ${i + 1} : ${t.toFixed(3)} s">
            <div class="s-label">S${i + 1}</div>
            <div class="s-value">${d == null ? t.toFixed(2) : fmtDelta(d)}</div>
        </div>`;
    }).join('');
}

function destroyTeleCharts() {
    teleCharts.forEach(c => c.destroy());
    teleCharts = [];
    $('tele-charts').innerHTML = '';
}

function trackLength() {
    return state.detail && state.detail.session.track_length_m;
}

function fmtDistance(v) {
    if (v == null) return '—';
    return trackLength() ? `${Math.round(v)} m` : `${v.toFixed(1)} %`;
}

function renderTeleCharts(data) {
    destroyTeleCharts();
    const length = trackLength();
    const x = data.d.map(d => length ? d * length : d * 100);
    const hasRef = !!data.ref;
    const lapColor = cssVar('--series-1');
    const refColor = cssVar('--series-2');
    const lapLap = data.lap_meta;
    const refLap = hasRef ? data.ref_meta : null;
    const lapName = lapLap ? `Tour ${lapLap.lap_number}` : 'Tour';
    const refName = !refLap ? 'Référence'
        : refLap.session_id !== lapLap.session_id ? `Record (${refLap.started_at.slice(0, 10)})` : `Réf. tour ${refLap.lap_number}`;

    const blocks = [];
    if (hasRef) {
        blocks.push({ title: `Delta (s) — au-dessus de 0 : ${lapName} perd du temps sur la référence`, height: 130,
            series: [{ label: 'Delta', values: data.delta, color: lapColor, fmt: v => `${fmtDelta(v)} s` }], zeroLine: true });
    }
    const channel = (title, key, height, fmt, scale = 1, extra = {}) => ({
        title, height, extra,
        series: [  // référence dessinée d'abord, le tour analysé reste au-dessus
            ...(hasRef ? [{ label: refName, values: data.ref[key].map(v => v * scale), color: refColor, fmt }] : []),
            { label: lapName, values: data.lap[key].map(v => v * scale), color: lapColor, fmt },
        ],
    });
    blocks.push(channel('Vitesse (km/h)', 'speed', 190, v => `${v.toFixed(0)} km/h`));
    blocks.push(channel('Accélérateur (%)', 'throttle', 150, v => `${v.toFixed(0)} %`, 100, { range: [0, 100] }));
    blocks.push(channel('Frein (%)', 'brake', 150, v => `${v.toFixed(0)} %`, 100, { range: [0, 100] }));
    blocks.push(channel('Rapport', 'gear', 110, v => `${v}`, 1, { stepped: true }));
    blocks.push(channel('Volant (°)', 'steer', 130, v => `${v.toFixed(0)}°`));

    const width = $('tele-charts').clientWidth;
    for (const block of blocks) {
        const wrap = document.createElement('div');
        wrap.className = 'tele-block';
        wrap.innerHTML = `<div class="tele-title">${escapeHtml(block.title)}</div>`;
        $('tele-charts').appendChild(wrap);
        const extra = block.extra || {};
        const chart = new uPlot({
            width,
            height: block.height,
            scales: {
                x: { time: false },
                y: extra.range ? { range: () => extra.range } : {},
            },
            cursor: { sync: { key: 'telemetry', setSeries: false }, drag: { x: true, y: false } },
            axes: [
                baseAxis({ values: (u, splits) => splits.map(fmtDistance) }),
                baseAxis({ size: 56 }),
            ],
            series: [
                { label: 'Distance', value: (u, v) => fmtDistance(v) },
                ...block.series.map(s => ({
                    label: s.label,
                    stroke: s.color,
                    width: 2,
                    points: { show: false },
                    paths: extra.stepped ? uPlot.paths.stepped({ align: 1 }) : undefined,
                    value: (u, v) => v == null ? '—' : s.fmt(v),
                })),
            ],
            hooks: {
                setCursor: [u => drawMapCursor(u.cursor.idx)],
                setScale: [(u, key) => {
                    if (key !== 'x' || syncingZoom) return;
                    syncingZoom = true;
                    const { min, max } = u.scales.x;
                    teleCharts.forEach(other => other !== u && other.setScale('x', { min, max }));
                    syncingZoom = false;
                }],
                draw: block.zeroLine ? [u => {
                    const y = Math.round(u.valToPos(0, 'y', true));
                    const ctx = u.ctx;
                    ctx.save();
                    ctx.strokeStyle = cssVar('--text-muted');
                    ctx.lineWidth = 1;
                    ctx.beginPath();
                    ctx.moveTo(u.bbox.left, y);
                    ctx.lineTo(u.bbox.left + u.bbox.width, y);
                    ctx.stroke();
                    ctx.restore();
                }] : [],
            },
        }, [x, ...block.series.map(s => s.values)], wrap);
        teleCharts.push(chart);
    }
}

// --- redimensionnement --------------------------------------------------------

new ResizeObserver(() => {
    const width = $('tele-charts').clientWidth;
    teleCharts.forEach(c => c.setSize({ width, height: c.height }));
    if (lapTimeChart) lapTimeChart.setSize({ width: $('laptime-chart').clientWidth, height: lapTimeChart.height });
}).observe($('main'));

// --- boucles de rafraîchissement ------------------------------------------------

let tickCount = 0;
async function tick() {
    await pollStatus();
    // nouveaux tours : une vérification par seconde suffit
    if (tickCount++ % 4 === 0 && state.sessionId && state.follow) await refreshDetail().catch(console.error);
}

refreshSessions().catch(console.error);
tick();
setInterval(tick, 250);  // statut en direct (barres gaz / frein) et nouveaux tours
setInterval(() => refreshSessions().catch(console.error), 3000);

// --- nom et note de la session --------------------------------------------------------

let saveTimer = null;
function scheduleSave() {
    clearTimeout(saveTimer);
    $('save-state').textContent = '…';
    saveTimer = setTimeout(saveSessionInfo, 600);
}
async function saveSessionInfo() {
    if (!state.sessionId) return;
    try {
        await api(`/api/sessions/${state.sessionId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: $('session-name').value, note: $('session-note').value }),
        });
        $('save-state').textContent = 'Enregistré ✓';
        if (state.detail) {
            state.detail.session.name = $('session-name').value.trim() || null;
            state.detail.session.note = $('session-note').value.trim() || null;
        }
        state.sessionsKey = null;
        refreshSessions().catch(console.error);
    } catch (err) {
        $('save-state').textContent = `Erreur : ${err.message}`;
    }
}
$('session-name').addEventListener('input', scheduleSave);
$('session-note').addEventListener('input', scheduleSave);
$('session-name').addEventListener('keydown', e => { if (e.key === 'Enter') e.target.blur(); });

// --- sauvegarde / restauration ----------------------------------------------------------

$('restore-file').addEventListener('change', async e => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    if (!confirm(`Restaurer « ${file.name} » ? Toutes les données actuelles seront remplacées ` +
                 `(une copie de sécurité est gardée dans le dossier data/).`)) return;
    const form = new FormData();
    form.append('file', file);
    try {
        const res = await api('/api/restore', { method: 'POST', body: form });
        alert(`Sauvegarde restaurée. Copie de sécurité de l'ancienne base : data/${res.safety_copy}`);
        location.reload();
    } catch (err) {
        alert(`Restauration impossible : ${err.message}`);
    }
});

// --- carte du circuit et virages ------------------------------------------------------

const mapState = { base: null, points: null, scale: 1 };

function hexToRgb(hex) {
    const n = parseInt(hex.replace('#', ''), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
function mixColor(a, b, t) {
    const ca = hexToRgb(a), cb = hexToRgb(b);
    return `rgb(${ca.map((v, i) => Math.round(v + (cb[i] - v) * t)).join(',')})`;
}

function renderAnalysis(data) {
    renderMap(data);
    renderCorners(data);
}

function renderMap(data) {
    const canvas = $('track-map');
    const ctx = canvas.getContext('2d');
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return;  // vue masquée : redessinée quand elle réapparaît
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    mapState.base = null;
    mapState.points = null;

    if (!data || !data.map) {
        $('map-legend').textContent = data ? 'Carte indisponible pour ce tour (tour enregistré avant l\'ajout de la carte).' : '';
        return;
    }
    const { x, y } = data.map;
    const pad = 22;
    const minX = Math.min(...x), maxX = Math.max(...x), minY = Math.min(...y), maxY = Math.max(...y);
    const scale = Math.min((width - 2 * pad) / (maxX - minX || 1), (height - 2 * pad) / (maxY - minY || 1));
    const offX = (width - (maxX - minX) * scale) / 2, offY = (height - (maxY - minY) * scale) / 2;
    // nord en haut : on inverse l'axe y de l'écran
    const pts = x.map((v, i) => [offX + (v - minX) * scale, height - (offY + (y[i] - minY) * scale)]);
    mapState.points = pts;

    // couleur : vitesse à laquelle le delta augmente (rouge = tu perds du temps, bleu = tu en gagnes)
    const n = pts.length;
    let slopes = null;
    if (data.delta) {
        const w = Math.max(1, Math.round(n / 200));
        slopes = data.delta.map((_, i) => data.delta[Math.min(n - 1, i + w)] - data.delta[Math.max(0, i - w)]);
        const sorted = slopes.map(Math.abs).sort((a, b) => a - b);
        const cap = sorted[Math.floor(sorted.length * 0.95)] || 1;
        slopes = slopes.map(v => Math.max(-1, Math.min(1, v / cap)));
    }
    const neutral = cssVar('--text-muted'), gain = cssVar('--gain'), loss = cssVar('--loss');
    ctx.lineWidth = 5;
    ctx.lineCap = 'round';
    const step = Math.max(1, Math.floor(n / 1500));
    for (let i = step; i < n; i += step) {
        const v = slopes ? slopes[i] : 0;
        ctx.strokeStyle = Math.abs(v) < 0.08 ? neutral : mixColor(neutral, v > 0 ? loss : gain, Math.min(1, Math.abs(v) * 1.3));
        ctx.beginPath();
        ctx.moveTo(...pts[i - step]);
        ctx.lineTo(...pts[i]);
        ctx.stroke();
    }
    // ligne de départ et numéros de virage
    ctx.fillStyle = cssVar('--text-primary');
    ctx.fillRect(pts[0][0] - 3, pts[0][1] - 3, 6, 6);
    ctx.font = '600 12px -apple-system, "Segoe UI", sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    const posToIndex = pos => Math.round((trackLength() ? pos / trackLength() : pos / 100) * (n - 1));
    (data.corners || []).forEach(c => {
        const [px, py] = pts[Math.max(0, Math.min(n - 1, posToIndex(c.lap.apex)))];
        ctx.fillStyle = cssVar('--surface-1');
        ctx.beginPath();
        ctx.arc(px, py - 14, 9, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = cssVar('--text-primary');
        ctx.fillText(String(c.number), px, py - 14);
    });
    mapState.base = ctx.getImageData(0, 0, canvas.width, canvas.height);
    $('map-legend').innerHTML = slopes
        ? `<span><span class="swatch" style="background:${loss}"></span>tu perds du temps</span>
           <span><span class="swatch" style="background:${gain}"></span>tu gagnes du temps</span>
           <span>■ ligne de départ</span>`
        : 'Choisis une référence pour colorer la carte selon le delta.';
}

function drawMapCursor(idx) {
    const canvas = $('track-map');
    if (!mapState.base || !mapState.points) return;
    const ctx = canvas.getContext('2d');
    ctx.putImageData(mapState.base, 0, 0);
    if (idx == null || !mapState.points[idx]) return;
    const [px, py] = mapState.points[idx];
    ctx.fillStyle = cssVar('--text-primary');
    ctx.strokeStyle = cssVar('--bg');
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(px, py, 6, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
}

function renderCorners(data) {
    const corners = data ? data.corners || [] : [];
    if (!corners.length) {
        $('corners').innerHTML = `<div class="empty small">${data ? 'Aucun virage détecté.' : ''}</div>`;
        return;
    }
    const hasRef = corners.some(c => c.ref);
    const worst = hasRef ? corners.filter(c => c.time_lost > 0.02)
        .sort((a, b) => b.time_lost - a.time_lost).slice(0, 3).map(c => c.number) : [];
    $('corners').innerHTML = corners.map(c => {
        const lost = c.time_lost;
        const lostHtml = lost == null ? '' : `<span class="${lost > 0 ? 'loss' : 'gain'}">${fmtDelta(lost)} s</span>`;
        const stats = `Freinage ${fmtDistance(c.lap.brake_point)} · mini ${Math.round(c.lap.min_speed)} km/h · gaz ${fmtDistance(c.lap.throttle_point)}`;
        const tips = c.advice.length ? `<ul>${c.advice.map(t => `<li>${escapeHtml(t)}</li>`).join('')}</ul>` : '';
        return `<button type="button" class="corner${worst.includes(c.number) ? ' priority' : ''}" data-start="${c.start}" data-end="${c.end}">
            <div class="corner-head"><span>Virage ${c.number}${worst.includes(c.number) ? '<span class="tag">à travailler</span>' : ''}</span>${lostHtml}</div>
            <div class="corner-stats">${stats}</div>
            ${tips}
        </button>`;
    }).join('');
}

$('corners').addEventListener('click', e => {
    const el = e.target.closest('.corner');
    if (!el || !teleCharts.length) return;
    const margin = trackLength() ? 60 : 1.5;
    teleCharts[0].setScale('x', { min: Number(el.dataset.start) - margin, max: Number(el.dataset.end) + margin });
    $('tele-charts').scrollIntoView({ behavior: 'smooth', block: 'start' });
});

new ResizeObserver(() => { if (state.compare) renderMap(state.compare); }).observe($('track-map'));

// --- débrief de la session ----------------------------------------------------------------

async function loadDebrief(sessionId) {
    let data;
    try {
        data = await api(`/api/sessions/${sessionId}/debrief`);
    } catch (err) {
        return;
    }
    if (sessionId !== state.sessionId) return;
    const item = r => `<li><div class="r-title">${escapeHtml(r.title)}</div>${r.detail ? `<div class="r-detail">${escapeHtml(r.detail)}</div>` : ''}</li>`;
    $('debrief-grid').hidden = !data.ready;
    $('debrief-empty').hidden = data.ready;
    $('debrief-empty').textContent = data.message || '';
    $('debrief-priority').hidden = !data.priority;
    if (data.priority) {
        $('debrief-priority').innerHTML = `<div class="p-label">🎯 Priorité pour la prochaine session</div>
            <div class="r-title">${escapeHtml(data.priority.title)}</div>
            <div class="r-detail">${escapeHtml(data.priority.detail)}</div>`;
    }
    $('debrief-good').innerHTML = data.good.map(item).join('') || '<li class="none">Rien de marquant pour l\'instant.</li>';
    $('debrief-bad').innerHTML = data.bad.map(item).join('') || '<li class="none">Rien à signaler, beau travail.</li>';
}
