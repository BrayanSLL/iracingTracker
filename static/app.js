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
                <div class="name">${escapeHtml(s.track || 'Circuit inconnu')}${s.id === liveId ? '<span class="live-tag">● en cours</span>' : ''}</div>
                <div class="meta">${escapeHtml(s.car || '')}</div>
                <div class="meta">${s.started_at.replace('T', ' ').slice(0, 16)} · ${escapeHtml(s.session_type || '')}</div>
                <div class="meta">${s.laps} tours · meilleur ${fmtTime(s.best_lap)}</div>
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

function renderDetail({ session, laps, stats }) {
    showSession(true);
    $('session-title').textContent = session.track || 'Circuit inconnu';
    $('session-subtitle').textContent = [session.car, session.session_type,
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
    if (!state.refPinned || !laps.some(l => l.id === state.refId)) {
        state.refId = defaultReference(laps, stats);
    }

    renderLapTimeChart(laps, stats);
    renderLapTable(laps, stats);
    renderSelectors(laps);
    loadCompare();
}

function defaultReference(laps, stats) {
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
            <td>${l.has_trace ? `<button class="btn ref-btn" type="button" data-ref="${l.id}" title="Utiliser comme référence">Réf.</button>` : ''}</td>
        </tr>`;
    }).join('');
}

$('laps-body').addEventListener('click', event => {
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
    $('ref-select').innerHTML = '<option value="">Aucune</option>' + options.map(l =>
        `<option value="${l.id}"${l.id === state.refId ? ' selected' : ''}>${lapLabel(l)}</option>`).join('');
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
    renderSectors();
    if (!state.lapId) {
        destroyTeleCharts();
        showTeleMessage('Aucun tour avec télémétrie dans cette session.');
        return;
    }
    let data;
    try {
        const refParam = state.refId ? `&ref=${state.refId}` : '';
        data = await api(`/api/compare?lap=${state.lapId}${refParam}`);
    } catch (err) {
        destroyTeleCharts();
        showTeleMessage(err.message);
        return;
    }
    if (key !== state.compareKey) return;  // sélection changée pendant le chargement
    showTeleMessage(null);
    renderTeleCharts(data);
}

function showTeleMessage(message) {
    $('tele-empty').hidden = !message;
    $('tele-empty').textContent = message || '';
}

function renderSectors() {
    const laps = state.detail ? state.detail.laps : [];
    const lap = laps.find(l => l.id === state.lapId);
    const ref = laps.find(l => l.id === state.refId);
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
    const lapLap = state.detail.laps.find(l => l.id === state.lapId);
    const refLap = hasRef ? state.detail.laps.find(l => l.id === state.refId) : null;
    const lapName = lapLap ? `Tour ${lapLap.lap_number}` : 'Tour';
    const refName = refLap ? `Réf. tour ${refLap.lap_number}` : 'Référence';

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
