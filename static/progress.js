'use strict';
// Progression : niveau, objectifs par voiture et par circuit, notifications de déblocage.
// Réutilise les utilitaires d'app.js ($, api, fmtTime, escapeHtml).

const progress = {
    view: 'sessions',
    car: null,
    track: null,
    lastUnlockId: null,  // null = pas encore initialisé (on ne notifie pas l'historique)
    hideDone: false,
};
try { progress.hideDone = localStorage.getItem('hideDone') === '1'; } catch (_) { /* stockage indisponible */ }
$('hide-done').checked = progress.hideDone;

const TIER_LABELS = { bronze: 'Bronze', argent: 'Argent', or: 'Or', platine: 'Platine' };

// --- navigation -------------------------------------------------------------------

function showView(view) {
    progress.view = view;
    $('view-sessions').hidden = view !== 'sessions';
    $('view-progress').hidden = view !== 'progress';
    $('view-compare').hidden = view !== 'compare';
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.view === view));
    if (view === 'progress') refreshProgress().catch(console.error);
    if (view === 'compare') refreshCompareView().catch(console.error);
}
document.querySelectorAll('[data-view]').forEach(el => el.addEventListener('click', () => showView(el.dataset.view)));

// --- niveau -----------------------------------------------------------------------

async function refreshProfile() {
    const p = await api('/api/profile');
    const pct = `${Math.round(p.level_xp / p.next_level_xp * 100)}%`;
    $('level-num').textContent = `Niv. ${p.level}`;
    $('level-fill').style.width = pct;
    $('profile-level').textContent = p.level;
    $('profile-title').textContent = p.title;
    $('profile-xp-fill').style.width = pct;
    $('profile-xp-text').textContent = `${p.level_xp} / ${p.next_level_xp} XP avant le niveau ${p.level + 1}`;
    $('profile-xp-total').textContent = p.xp;
    $('profile-completed').textContent = `${p.completed} / ${p.total}`;
    return p;
}

// --- objectifs --------------------------------------------------------------------

function fmtValue(v, unit) {
    if (v == null) return '—';
    if (unit === 's') return `${v.toFixed(3)} s`;
    if (unit === '%') return `${v.toFixed(2)} %`;
    if (unit === 'km' || unit === 'h') return `${v.toFixed(1)} ${unit}`;
    if (unit === 'L') return `${Math.round(v)} L`;
    return `${Math.floor(v)} ${unit}`;
}

function objectiveCard(o) {
    const done = !!o.completed_at;
    let foot;
    if (done) {
        foot = `<span class="obj-done-label">✓ Réussi</span><span>${o.completed_at.slice(0, 10)}</span>`;
    } else if (o.hint === 'baseline') {
        foot = `<span>Fais 3 tours propres pour établir ta référence</span>`;
    } else {
        const target = o.compare === 'lte' ? `objectif ≤ ${fmtValue(o.target, o.unit)}` : `objectif ${fmtValue(o.target, o.unit)}`;
        foot = `<span>${fmtValue(o.value, o.unit)}</span><span>${target}</span>`;
    }
    const extra = o.hint === 'ref' && o.target_time
        ? `<div class="obj-foot"><span>Temps cible</span><span>${fmtTime(o.target_time)}</span></div>` : '';
    return `<div class="obj${done ? ' done' : ''}">
        <div class="obj-head">
            <div class="obj-title">${escapeHtml(o.title)}</div>
            <div class="obj-xp">+${o.xp} XP</div>
        </div>
        <div class="tier ${o.tier}">${TIER_LABELS[o.tier] || o.tier}</div>
        <div class="obj-bar" role="progressbar" aria-valuenow="${Math.round(o.progress * 100)}" aria-valuemin="0" aria-valuemax="100">
            <span style="width:${Math.round(o.progress * 100)}%"></span>
        </div>
        <div class="obj-foot">${foot}</div>
        ${extra}
    </div>`;
}

function renderObjectives(container, objectives) {
    const visible = objectives.filter(o => !(progress.hideDone && o.completed_at));
    if (!visible.length) {
        container.innerHTML = '<div class="empty small">Tous les objectifs sont réussis 🎉</div>';
        return;
    }
    const categories = [];
    visible.forEach(o => { if (!categories.includes(o.category)) categories.push(o.category); });
    container.innerHTML = categories.map(cat => {
        const items = visible.filter(o => o.category === cat)
            .sort((a, b) => !!a.completed_at - !!b.completed_at);  // à faire d'abord
        return `<div class="obj-category">${escapeHtml(cat)}</div><div class="obj-grid">${items.map(objectiveCard).join('')}</div>`;
    }).join('');
}

function chip(label, meta, selected, attr) {
    return `<button class="chip${selected ? ' selected' : ''}" type="button" ${attr}>${escapeHtml(label)}<span class="chip-meta">${meta}</span></button>`;
}

async function refreshProgress() {
    await refreshProfile();
    const cars = await api('/api/cars');
    $('no-cars').hidden = cars.length > 0;
    $('progress-content').hidden = cars.length === 0;
    if (!cars.length) return;

    if (!cars.some(c => c.name === progress.car)) {
        const live = state.status && state.status.connected ? state.status.car : null;
        progress.car = cars.some(c => c.name === live) ? live : cars[0].name;
        progress.track = null;
    }
    $('car-chips').innerHTML = cars.map(c =>
        chip(c.name, `${c.completed}/${c.total} · ${c.xp} XP`, c.name === progress.car, `data-car="${escapeHtml(c.name)}"`)).join('');
    $('car-objectives-title').textContent = `Objectifs — ${progress.car}`;

    const carData = await api(`/api/objectives?car=${encodeURIComponent(progress.car)}`);
    renderObjectives($('car-objectives'), carData.objectives);

    const tracks = await api(`/api/cars/${encodeURIComponent(progress.car)}/tracks`);
    $('no-tracks').hidden = tracks.length > 0;
    if (!tracks.some(t => t.track === progress.track)) progress.track = tracks.length ? tracks[0].track : null;
    $('track-chips').innerHTML = tracks.map(t =>
        chip(t.track, `${t.completed}/${t.total}`, t.track === progress.track, `data-track="${escapeHtml(t.track)}"`)).join('');

    if (progress.track) {
        const data = await api(`/api/objectives?car=${encodeURIComponent(progress.car)}&track=${encodeURIComponent(progress.track)}`);
        $('track-info').textContent = data.baseline
            ? `Ta référence : ${fmtTime(data.baseline)} (meilleur de tes 3 premiers tours propres) · ton record : ${fmtTime(data.pb)}`
            : 'Référence pas encore établie : fais 3 tours propres sur ce circuit.';
        renderObjectives($('track-objectives'), data.objectives);
        renderRecordChart(await api(`/api/records?car=${encodeURIComponent(progress.car)}&track=${encodeURIComponent(progress.track)}`));
        renderProgressDebrief(await api(`/api/progress-debrief?car=${encodeURIComponent(progress.car)}&track=${encodeURIComponent(progress.track)}`));
    } else {
        renderRecordChart([]);
        renderProgressDebrief(null);
        $('track-info').textContent = '';
        $('track-objectives').innerHTML = '';
    }
}

$('car-chips').addEventListener('click', e => {
    const el = e.target.closest('[data-car]');
    if (!el) return;
    progress.car = el.dataset.car;
    progress.track = null;
    refreshProgress().catch(console.error);
});
$('track-chips').addEventListener('click', e => {
    const el = e.target.closest('[data-track]');
    if (!el) return;
    progress.track = el.dataset.track;
    refreshProgress().catch(console.error);
});
$('hide-done').addEventListener('change', e => {
    progress.hideDone = e.target.checked;
    try { localStorage.setItem('hideDone', progress.hideDone ? '1' : '0'); } catch (_) { /* ignoré */ }
    refreshProgress().catch(console.error);
});

// --- notifications ------------------------------------------------------------------

function toast(title, sub) {
    const el = document.createElement('div');
    el.className = 'toast';
    el.innerHTML = `<div class="toast-title">${escapeHtml(title)}</div><div class="toast-sub">${escapeHtml(sub)}</div>`;
    $('toasts').appendChild(el);
    setTimeout(() => el.remove(), 6000);
}

async function pollUnlocks() {
    const events = await api(`/api/unlocks?after=${progress.lastUnlockId || 0}`);
    if (!events.length) {
        if (progress.lastUnlockId === null) progress.lastUnlockId = 0;
        return;
    }
    const first = progress.lastUnlockId === null;
    progress.lastUnlockId = events[events.length - 1].id;
    if (first) return;  // au lancement : l'historique n'est pas notifié

    const before = parseInt($('level-num').textContent.replace(/\D/g, ''), 10);
    events.slice(-4).forEach(e => toast(`🏆 ${e.title}`, `+${e.xp} XP · ${e.car}${e.track ? ' · ' + e.track : ''}`));
    if (events.length > 4) toast(`… et ${events.length - 4} autres objectifs`, '');
    const p = await refreshProfile();
    if (p.level > before) toast(`⬆️ Niveau ${p.level} !`, p.title);
    if (progress.view === 'progress') refreshProgress().catch(console.error);
}

refreshProfile().catch(console.error);
pollUnlocks().catch(console.error);
setInterval(() => pollUnlocks().catch(console.error), 2000);

// --- historique du record -------------------------------------------------------------

let recordChart = null;

function renderRecordChart(rows) {
    if (recordChart) { recordChart.destroy(); recordChart = null; }
    $('record-box').hidden = rows.length === 0;
    if (!rows.length) return;
    const container = $('record-chart');
    const x = rows.map(r => new Date(r.started_at).getTime() / 1000);
    if (x.length === 1) x.push(x[0] + 3600);  // une seule session : on étire un peu l'axe
    const record = rows.map(r => r.record);
    const best = rows.map(r => r.session_best);
    if (record.length < x.length) { record.push(record[record.length - 1]); best.push(null); }
    const dateFmt = v => new Date(v * 1000).toLocaleDateString('fr-FR', { day: '2-digit', month: 'short' });
    recordChart = new uPlot({
        width: container.clientWidth,
        height: 240,
        scales: { x: { time: true }, y: { range: padRange(0.5) } },
        cursor: { drag: { x: false, y: false } },
        axes: [
            baseAxis({ values: (u, splits) => splits.map(dateFmt) }),
            baseAxis({ size: 70, values: (u, splits) => splits.map(v => fmtTime(v)) }),
        ],
        series: [
            { label: 'Date', value: (u, v) => v == null ? '—' : new Date(v * 1000).toLocaleString('fr-FR', { dateStyle: 'medium', timeStyle: 'short' }) },
            { label: 'Record', stroke: cssVar('--series-1'), width: 2, paths: uPlot.paths.stepped({ align: 1 }),
              points: { show: true, size: 6, fill: cssVar('--series-1') }, value: (u, v) => fmtTime(v) },
            { label: 'Meilleur tour de la session', stroke: cssVar('--series-2'), paths: () => null,
              points: { show: true, size: 8, fill: cssVar('--series-2'), stroke: cssVar('--surface-1'), width: 2 },
              value: (u, v) => fmtTime(v) },
        ],
    }, [x, record, best], container);
}

new ResizeObserver(() => {
    if (recordChart) recordChart.setSize({ width: $('record-chart').clientWidth, height: recordChart.height });
}).observe($('view-progress'));
