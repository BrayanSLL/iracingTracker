'use strict';
// Coaching : annonces vocales, mode entraînement, comparaison de sessions, débrief de progression.
// Réutilise app.js ($, api, state, fmtTime, fmtDelta, escapeHtml…) et progress.js (showView, toast).

// --- annonces vocales ---------------------------------------------------------------------

api('/api/settings').then(s => { $('voice-mode').value = s.voice; }).catch(console.error);
$('voice-mode').addEventListener('change', e => {
    api('/api/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ voice: e.target.value }),
    }).catch(err => alert(err.message));
});

// --- mode entraînement --------------------------------------------------------------------

async function startTraining(cornerEl) {
    const session = state.detail && state.detail.session;
    if (!session || !cornerEl) return;
    const toFraction = pos => trackLength() ? pos / trackLength() : pos / 100;
    const number = Number(cornerEl.dataset.number);
    try {
        const training = await api('/api/training', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                car: session.car, track: session.track, number,
                d0: Math.max(0, toFraction(Number(cornerEl.dataset.start))),
                d1: Math.min(1, toFraction(Number(cornerEl.dataset.end))),
            }),
        });
        if (state.status) state.status.training = training;
        toast(`🎯 Entraînement : virage ${number}`, 'Chaque passage sera chronométré et comparé à ton record.');
        renderCorners(state.compare);
        renderTrainingBanner();
    } catch (err) {
        alert(err.message);
    }
}

async function stopTraining() {
    await api('/api/training', { method: 'DELETE' });
    if (state.status) state.status.training = null;
    renderTrainingBanner();
    renderCorners(state.compare);
}

let trainingKey = null;
function renderTrainingBanner() {
    const tr = state.status && state.status.training;
    const banner = $('training-banner');
    if (!tr) {
        banner.hidden = true;
        trainingKey = null;
        return;
    }
    const key = JSON.stringify(tr);
    if (key === trainingKey) return;
    trainingKey = key;
    banner.hidden = false;
    const last = tr.attempts[tr.attempts.length - 1];
    const chips = tr.attempts.map(a => {
        const cls = a.delta == null ? '' : a.delta > 0 ? 'loss' : 'gain';
        const label = a.delta == null ? `${a.time.toFixed(3)} s` : `${fmtDelta(a.delta, 2)}`;
        return `<span class="attempt ${cls}${a.personal_best ? ' pb' : ''}" title="Tour ${a.lap} : ${a.time.toFixed(3)} s">${label}</span>`;
    }).join('');
    const live = tr.in_zone && tr.live != null ? ` · en cours <b>${fmtDelta(tr.live, 2)}</b>` : '';
    banner.innerHTML = `
        <div>
            <div class="t-title">🎯 Entraînement : virage ${tr.number}</div>
            <div class="t-stat">${tr.active ? '' : `En attente de ${escapeHtml(tr.car || '?')} sur ${escapeHtml(tr.track || '?')} · `}
                ${tr.count} passage(s) · meilleur <b>${tr.best != null ? tr.best.toFixed(3) + ' s' : '—'}</b>
                · record <b>${tr.ref_time != null ? tr.ref_time.toFixed(3) + ' s' : '—'}</b>
                ${last && last.delta != null ? ` · dernier <b>${fmtDelta(last.delta, 2)}</b>` : ''}${live}</div>
        </div>
        <div class="attempts" aria-label="Derniers passages">${chips}</div>
        <button class="btn" type="button" id="stop-training">Arrêter l'entraînement</button>`;
    $('stop-training').addEventListener('click', () => stopTraining().catch(console.error));
}
setInterval(renderTrainingBanner, 300);

// --- comparaison de deux sessions ---------------------------------------------------------

const compareState = { sessions: [], a: null, b: null, key: null };

function sessionLabel(s) {
    return `#${s.id} · ${s.started_at.slice(0, 16).replace('T', ' ')} · ${s.name || s.track || '?'} · ${s.car || ''} · ${s.laps} tours`;
}

$('compare-btn').addEventListener('click', async () => {
    const current = state.detail && state.detail.session;
    if (!current) return;
    const sessions = await api('/api/sessions');
    const previous = sessions.find(s => s.id !== current.id && s.car === current.car && s.track === current.track
        && s.started_at <= current.started_at);
    const other = previous || sessions.find(s => s.id !== current.id && s.car === current.car && s.track === current.track);
    compareState.b = current.id;
    compareState.a = other ? other.id : null;
    showView('compare');
});

async function refreshCompareView() {
    compareState.sessions = await api('/api/sessions');
    const sessions = compareState.sessions;
    if (!sessions.some(s => s.id === compareState.a)) compareState.a = sessions[1] ? sessions[1].id : null;
    const a = sessions.find(s => s.id === compareState.a);
    const candidates = a ? sessions.filter(s => s.id !== a.id && s.car === a.car && s.track === a.track) : [];
    if (!candidates.some(s => s.id === compareState.b)) compareState.b = candidates.length ? candidates[0].id : null;

    $('compare-a').innerHTML = sessions.map(s =>
        `<option value="${s.id}"${s.id === compareState.a ? ' selected' : ''}>${escapeHtml(sessionLabel(s))}</option>`).join('');
    $('compare-b').innerHTML = candidates.length
        ? candidates.map(s => `<option value="${s.id}"${s.id === compareState.b ? ' selected' : ''}>${escapeHtml(sessionLabel(s))}</option>`).join('')
        : '<option value="">Aucune autre session sur ce circuit avec cette voiture</option>';
    await loadComparison();
}

$('compare-a').addEventListener('change', e => { compareState.a = Number(e.target.value); refreshCompareView().catch(console.error); });
$('compare-b').addEventListener('change', e => { compareState.b = Number(e.target.value) || null; loadComparison().catch(console.error); });
$('compare-swap').addEventListener('click', () => {
    if (!compareState.a || !compareState.b) return;
    [compareState.a, compareState.b] = [compareState.b, compareState.a];
    refreshCompareView().catch(console.error);
});

async function loadComparison() {
    const ready = compareState.a && compareState.b;
    $('compare-result').hidden = !ready;
    $('compare-empty').hidden = !!ready;
    if (!ready) return;
    const data = await api(`/api/compare-sessions?a=${compareState.a}&b=${compareState.b}`);
    renderComparison(data);
}

function renderComparison(data) {
    const head = (tag, s) => `<div class="compare-head">
        <div class="tag-ab">${tag}</div>
        <div class="c-name">${escapeHtml(s.name || s.track || '?')}</div>
        <div class="muted">${escapeHtml([s.name ? s.track : null, s.car, s.session_type, s.started_at.slice(0, 16).replace('T', ' ')].filter(Boolean).join(' · '))}</div>
        ${s.note ? `<div class="c-note">« ${escapeHtml(s.note)} »</div>` : ''}
    </div>`;
    $('compare-heads').innerHTML = head('SESSION A · AVANT', data.a) + head('SESSION B · APRÈS', data.b);
    $('compare-verdict').innerHTML = data.verdict.map(v => `<li class="${v.tone}">${escapeHtml(v.text)}</li>`).join('')
        || '<li>Pas assez de tours propres pour comparer.</li>';

    const fmtMetric = (key, v) => {
        if (v == null) return '—';
        if (key === 'stdev') return `± ${v.toFixed(3)} s`;
        if (key === 'clean_laps') return String(v);
        if (key === 'avg_fuel') return `${v.toFixed(2)} L`;
        return fmtTime(v);
    };
    const fmtDiff = (key, d) => {
        if (d == null) return '—';
        if (key === 'clean_laps') return `${d > 0 ? '+' : ''}${d}`;
        if (key === 'avg_fuel') return `${d > 0 ? '+' : ''}${d.toFixed(2)} L`;
        return `${fmtDelta(d)} s`;
    };
    $('compare-metrics').innerHTML = data.metrics.map(m => `<tr>
        <td>${escapeHtml(m.label)}</td><td>${fmtMetric(m.key, m.a)}</td><td>${fmtMetric(m.key, m.b)}</td>
        <td class="${m.better === true ? 'better' : m.better === false ? 'worse' : ''}">${fmtDiff(m.key, m.diff)}</td>
    </tr>`).join('');

    $('compare-sectors-panel').hidden = !data.sectors;
    if (data.sectors) {
        $('compare-sectors').innerHTML = data.sectors.map((d, i) => {
            const tone = Math.min(100, Math.round(Math.abs(d) / 0.25 * 100));
            const color = Math.abs(d) < 0.0005 ? 'var(--neutral)'
                : `color-mix(in srgb, ${d > 0 ? 'var(--loss)' : 'var(--gain)'} ${Math.max(tone, 25)}%, var(--neutral))`;
            return `<div class="sector" style="background:${color}"><div class="s-label">S${i + 1}</div><div class="s-value">${fmtDelta(d)}</div></div>`;
        }).join('');
    }

    $('compare-corners-panel').hidden = !data.corners.length;
    $('compare-corners').innerHTML = data.corners.map(c => `<div class="corner">
        <div class="corner-head"><span>Virage ${c.number}</span>
            <span class="${c.time_lost > 0 ? 'loss' : 'gain'}">${fmtDelta(c.time_lost)} s</span></div>
        <div class="corner-stats">Vitesse mini ${Math.round(c.a.min_speed)} → ${Math.round(c.b.min_speed)} km/h</div>
        ${c.advice.length ? `<ul>${c.advice.map(t => `<li>${escapeHtml(t.replace(/^Tu /, 'En B, tu '))}</li>`).join('')}</ul>` : ''}
    </div>`).join('');
}

// --- débrief de progression (onglet Progression) ------------------------------------------

function renderProgressDebrief(data) {
    const box = $('progress-debrief');
    box.hidden = !data;
    if (!data) return;
    const item = r => `<li><div class="r-title">${escapeHtml(r.title)}</div>${r.detail ? `<div class="r-detail">${escapeHtml(r.detail)}</div>` : ''}</li>`;
    $('progress-grid').hidden = !data.ready;
    $('progress-empty').hidden = data.ready;
    $('progress-empty').textContent = data.message || '';
    $('progress-period').textContent = data.period
        ? `— ${data.period.sessions} sessions, du ${data.period.since.slice(0, 10)} au ${data.period.until.slice(0, 10)}` : '';
    $('progress-priority').hidden = !data.priority;
    if (data.priority) {
        $('progress-priority').innerHTML = `<div class="p-label">🎯 À travailler en priorité</div>
            <div class="r-title">${escapeHtml(data.priority.title)}</div><div class="r-detail">${escapeHtml(data.priority.detail)}</div>`;
    }
    $('progress-good').innerHTML = data.good.map(item).join('') || '<li class="none">Rien de marquant pour l\'instant.</li>';
    $('progress-bad').innerHTML = data.bad.map(item).join('') || '<li class="none">Rien ne stagne, continue comme ça.</li>';
}
