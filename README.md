# iRacing Telemetry Logger

Enregistre automatiquement tes tours iRacing et affiche-les dans un petit dashboard web local. 🏁

> **Windows uniquement** : iRacing ne tourne que sous Windows, et ses données ne sont
> accessibles que depuis la même machine.

## Ce que fait le projet

- ✅ Lit la télémétrie d'iRacing en direct (60 fois par seconde) via [`pyirsdk`](https://github.com/kutu/pyirsdk)
- ✅ Détecte chaque tour terminé et enregistre son temps officiel
- ✅ Calcule par tour : carburant consommé, vitesse max, % moyen d'accélérateur et de frein
- ✅ Signale les tours « stand » (passage par la pitlane) et « partiels » (début d'enregistrement en cours de tour)
- ✅ Sauvegarde tout dans une base SQLite : l'historique survit au redémarrage
- ✅ Dashboard web : vitesse et carburant en direct, liste des tours, historique des sessions
- ✅ Export CSV d'une session

## Comment ça marche

iRacing **n'envoie pas de données sur le réseau**. Il publie sa télémétrie dans une
**zone de mémoire partagée** Windows (`Local\IRSDKMemoryMap`), mise à jour 60 fois par seconde.
La position de chaque variable dans cette mémoire n'est pas fixe : il faut lire les en-têtes
pour trouver où se trouve `Speed`, `LapLastLapTime`, etc. La bibliothèque `pyirsdk` s'en charge,
et on accède simplement aux valeurs par leur nom : `ir['Speed']`.

```
iRacing ──(mémoire partagée, 60 Hz)──▶ telemetry.py ──▶ data/sessions.db (SQLite)
                                                              │
                          navigateur ◀──(JSON)── main.py (Flask) ◀┘
```

---

## Installation

### 1. Prérequis

- Windows avec iRacing installé
- Python 3.8+

### 2. Création du projet

```bash
mkdir iracing-telemetry
cd iracing-telemetry

# Environnement virtuel (recommandé)
python -m venv venv
venv\Scripts\activate

# Dépendances
pip install flask pyirsdk
```

### 3. Structure

```
iracing-telemetry/
├── main.py              (serveur web + API)
├── telemetry.py         (lecture iRacing + détection des tours)
├── db.py                (base SQLite)
├── templates/
│   └── index.html       (dashboard ; Flask le cherche obligatoirement dans templates/)
└── data/
    └── sessions.db      (créé automatiquement au premier lancement)
```

Pense à ajouter `data/` à ton `.gitignore` pour ne pas versionner tes sessions.

---

## Code — Partie 1 : base de données

Crée `db.py` :

```python
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "sessions.db"


def connect():
    DB_PATH.parent.mkdir(exist_ok=True)  # crée data/ si besoin
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def execute(sql, params=()):
    """Exécute une écriture et renvoie l'id de la ligne insérée."""
    conn = connect()
    try:
        with conn:  # commit automatique
            return conn.execute(sql, params).lastrowid
    finally:
        conn.close()


def query(sql, params=()):
    """Exécute une lecture et renvoie une liste de dicts."""
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def init_db():
    conn = connect()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                track TEXT,
                car TEXT,
                session_type TEXT
            );
            CREATE TABLE IF NOT EXISTS laps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                lap_number INTEGER NOT NULL,
                lap_time REAL,          -- secondes, NULL si iRacing ne donne pas de temps
                fuel_used REAL,         -- litres consommés sur le tour
                fuel_left REAL,         -- litres restants à la fin du tour
                max_speed_kmh REAL,
                throttle_avg REAL,      -- 0..1
                brake_avg REAL,         -- 0..1
                flag TEXT,              -- NULL = tour propre, 'pit' ou 'partial'
                recorded_at TEXT NOT NULL
            );
        """)
    finally:
        conn.close()


def now():
    return datetime.now().isoformat(timespec="seconds")
```

---

## Code — Partie 2 : lecture de la télémétrie

Crée `telemetry.py` :

```python
import threading
import time

import irsdk  # pip install pyirsdk

import db

MS_TO_KMH = 3.6
LAP_TIME_TIMEOUT = 3.0  # secondes d'attente max pour que LapLastLapTime se mette à jour


class TelemetryRecorder:
    """Lit la mémoire partagée d'iRacing (via pyirsdk) et enregistre chaque tour."""

    def __init__(self):
        self.ir = irsdk.IRSDK()
        self.lock = threading.Lock()
        self.connected = False
        self.session_id = None
        self.track = None
        self.car = None
        self.live = {}
        self._reset_lap_tracking()

    # --- état interne -------------------------------------------------------

    def _reset_lap_tracking(self):
        self.session_num = None
        self.last_lap_completed = None
        self.prev_last_lap_time = None
        self.pending = None  # tour terminé qui attend son temps officiel
        self._reset_lap_stats()

    def _reset_lap_stats(self, flag=None, fuel_start=None):
        self.samples = 0
        self.throttle_sum = 0.0
        self.brake_sum = 0.0
        self.max_speed = 0.0
        self.flag = flag
        self.fuel_start = fuel_start

    # --- connexion ----------------------------------------------------------

    def _check_connection(self):
        if self.connected and not (self.ir.is_initialized and self.ir.is_connected):
            self.ir.shutdown()
            with self.lock:
                self.connected = False
                self.session_id = None
                self.live = {}
            print("[telemetry] iRacing déconnecté")
        elif not self.connected and self.ir.startup() and self.ir.is_initialized and self.ir.is_connected:
            with self.lock:
                self.connected = True
            self._reset_lap_tracking()
            print("[telemetry] iRacing connecté")
        return self.connected

    def _start_session(self, session_num):
        weekend = self.ir["WeekendInfo"] or {}
        driver_info = self.ir["DriverInfo"] or {}
        sessions = (self.ir["SessionInfo"] or {}).get("Sessions", [])

        car_idx = driver_info.get("DriverCarIdx")
        car = next((d.get("CarScreenName") for d in driver_info.get("Drivers", [])
                    if d.get("CarIdx") == car_idx), None)
        session_type = next((s.get("SessionType") for s in sessions
                             if s.get("SessionNum") == session_num), None)
        track = weekend.get("TrackDisplayName")

        session_id = db.execute(
            "INSERT INTO sessions (started_at, track, car, session_type) VALUES (?, ?, ?, ?)",
            (db.now(), track, car, session_type),
        )
        with self.lock:
            self.session_id, self.track, self.car = session_id, track, car
        print(f"[telemetry] nouvelle session #{session_id} : {track} / {car} ({session_type})")

    # --- boucle -------------------------------------------------------------

    def _tick(self):
        ir = self.ir
        ir.freeze_var_buffer_latest()  # toutes les lectures viennent du même tick
        try:
            session_num = ir["SessionNum"]
            if session_num is None:
                return
            if session_num != self.session_num:
                self._reset_lap_tracking()
                self.session_num = session_num
                self._start_session(session_num)

            last_lap_time = ir["LapLastLapTime"]
            fuel = ir["FuelLevel"]
            speed_kmh = (ir["Speed"] or 0.0) * MS_TO_KMH
            on_track = bool(ir["IsOnTrack"])
            on_pit_road = bool(ir["OnPitRoad"])

            with self.lock:
                self.live = {"on_track": on_track, "speed_kmh": speed_kmh,
                             "fuel_l": fuel, "lap": ir["Lap"]}

            # 1. Un tour vient de finir : on attend que LapLastLapTime change
            #    (il est mis à jour avec un léger retard après la ligne).
            if self.pending:
                waited = time.monotonic() - self.pending["since"]
                if last_lap_time != self.pending["prev_time"] or waited > LAP_TIME_TIMEOUT:
                    self._save_lap(self.pending, last_lap_time)
                    self.pending = None

            # 2. Au garage / dans les menus / en replay : rien à compter.
            if not on_track:
                self.last_lap_completed = None
                self.prev_last_lap_time = last_lap_time
                return

            lap_completed = ir["LapCompleted"]

            # 3. Première lecture en piste : on démarre en cours de tour.
            if self.last_lap_completed is None:
                self.last_lap_completed = lap_completed
                self._reset_lap_stats(flag="pit" if on_pit_road else "partial", fuel_start=fuel)

            # 4. Passage de ligne.
            elif lap_completed > self.last_lap_completed:
                fuel_used = None
                if self.fuel_start is not None and fuel is not None and self.fuel_start >= fuel:
                    fuel_used = self.fuel_start - fuel
                self.pending = {
                    "lap_number": lap_completed,
                    "prev_time": self.prev_last_lap_time,
                    "since": time.monotonic(),
                    "fuel_used": fuel_used,
                    "fuel_left": fuel,
                    "max_speed_kmh": self.max_speed,
                    "throttle_avg": self.throttle_sum / self.samples if self.samples else None,
                    "brake_avg": self.brake_sum / self.samples if self.samples else None,
                    "flag": self.flag,
                }
                self.last_lap_completed = lap_completed
                self._reset_lap_stats(fuel_start=fuel)

            # Compteur qui recule (reset, nouvelle voiture…) : tour partiel.
            elif lap_completed < self.last_lap_completed:
                self.last_lap_completed = lap_completed
                self._reset_lap_stats(flag="partial", fuel_start=fuel)

            # 5. Statistiques du tour en cours.
            self.samples += 1
            self.throttle_sum += ir["Throttle"] or 0.0
            self.brake_sum += ir["Brake"] or 0.0
            self.max_speed = max(self.max_speed, speed_kmh)
            if on_pit_road:
                self.flag = "pit"

            self.prev_last_lap_time = last_lap_time
        finally:
            ir.unfreeze_var_buffer_latest()

    def _save_lap(self, lap, lap_time):
        if lap_time is not None and lap_time <= 0:
            lap_time = None  # iRacing renvoie -1 quand il n'y a pas de temps valide
        db.execute(
            """INSERT INTO laps (session_id, lap_number, lap_time, fuel_used, fuel_left,
                                 max_speed_kmh, throttle_avg, brake_avg, flag, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.session_id, lap["lap_number"], lap_time, lap["fuel_used"], lap["fuel_left"],
             lap["max_speed_kmh"], lap["throttle_avg"], lap["brake_avg"], lap["flag"], db.now()),
        )

    def run(self):
        while True:
            try:
                if self._check_connection():
                    self._tick()
            except Exception as exc:  # on ne veut jamais tuer le thread
                print(f"[telemetry] erreur : {exc!r}")
                time.sleep(1)
            time.sleep(1 / 60)  # iRacing publie à 60 Hz

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def status(self):
        with self.lock:
            return {"connected": self.connected, "session_id": self.session_id,
                    "track": self.track, "car": self.car, **self.live}
```

### Pourquoi c'est fait comme ça

- **Détection du tour :** on surveille `LapCompleted`. Quand il augmente, le tour est terminé.
- **Temps du tour :** au moment où on passe la ligne, `LapLastLapTime` contient souvent encore
  le temps du tour *précédent*. On attend donc qu'il change (3 s maximum) avant d'enregistrer.
  Si iRacing ne donne pas de temps valide (il renvoie `-1`, par exemple sur un tour invalidé), on stocke `NULL`.
- **Stats du tour :** accélérateur et frein sont moyennés sur tous les échantillons du tour,
  la vitesse max est la plus haute vue, et le carburant consommé est la différence entre le début et la fin du tour.
- **Tours marqués :** `pit` si tu es passé par la pitlane (out-lap, in-lap, arrêt) et `partial` si
  l'enregistrement a commencé en plein tour. Ces tours sont exclus du meilleur tour et de la moyenne.
- **Sessions :** une nouvelle session est créée en base à chaque connexion à iRacing et à chaque
  changement de session (essais → qualif → course), avec le circuit, la voiture et le type de session.

---

## Code — Partie 3 : serveur web

Crée `main.py` :

```python
import argparse
import csv
import io

from flask import Flask, Response, jsonify, render_template

import db
from telemetry import TelemetryRecorder

app = Flask(__name__)
recorder = TelemetryRecorder()

LAP_COLUMNS = ["lap_number", "lap_time", "fuel_used", "fuel_left", "max_speed_kmh",
               "throttle_avg", "brake_avg", "flag", "recorded_at"]


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def status():
    return jsonify(recorder.status())


@app.get("/api/sessions")
def sessions():
    # meilleur tour et moyenne calculés uniquement sur les tours propres
    return jsonify(db.query("""
        SELECT s.*,
               COUNT(l.id) AS laps,
               MIN(CASE WHEN l.flag IS NULL THEN l.lap_time END) AS best_lap,
               AVG(CASE WHEN l.flag IS NULL THEN l.lap_time END) AS avg_lap
        FROM sessions s
        LEFT JOIN laps l ON l.session_id = s.id
        GROUP BY s.id
        ORDER BY s.id DESC
        LIMIT 50
    """))


@app.get("/api/sessions/<int:session_id>/laps")
def laps(session_id):
    return jsonify(db.query(
        "SELECT * FROM laps WHERE session_id = ? ORDER BY lap_number", (session_id,)))


@app.get("/api/sessions/<int:session_id>/export.csv")
def export_csv(session_id):
    rows = db.query(
        f"SELECT {', '.join(LAP_COLUMNS)} FROM laps WHERE session_id = ? ORDER BY lap_number",
        (session_id,))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=LAP_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return Response(out.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename=iracing-session-{session_id}.csv"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="iRacing Telemetry Logger")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    db.init_db()
    recorder.start()

    print(f"🏁 iRacing Telemetry Server démarré sur http://localhost:{args.port}")
    print("   Lance iRacing et monte en piste !")
    app.run(host="127.0.0.1", port=args.port)
```

### API disponible

| Route | Rôle |
|---|---|
| `GET /api/status` | Connexion iRacing, circuit, voiture, vitesse et carburant en direct |
| `GET /api/sessions` | Les 50 dernières sessions avec nombre de tours, meilleur tour et moyenne |
| `GET /api/sessions/<id>/laps` | Tous les tours d'une session |
| `GET /api/sessions/<id>/export.csv` | Export CSV d'une session |

---

## Code — Partie 4 : dashboard

Crée `templates/index.html` :

```html
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>iRacing Telemetry</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #1a1a1a; color: #fff; padding: 2rem 1rem;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { margin-bottom: 1.5rem; font-size: 28px; }
        .grid {
            display: grid; gap: 1rem; margin-bottom: 1.5rem;
            grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
        }
        .card { background: #2a2a2a; border-left: 4px solid #378ADD; padding: 1rem 1.25rem; border-radius: 8px; }
        .label { font-size: 12px; color: #999; margin-bottom: 6px; text-transform: uppercase; }
        .value { font-size: 22px; font-weight: 600; overflow-wrap: anywhere; }
        .value.small { font-size: 16px; }
        .active { color: #4ade80; }
        .inactive { color: #999; }
        .controls { display: flex; flex-wrap: wrap; gap: 1rem; margin-bottom: 1.5rem; align-items: center; }
        select, .button {
            background: #2a2a2a; color: #fff; border: 1px solid #444;
            padding: 10px 14px; border-radius: 6px; font-size: 14px;
        }
        .button { background: #378ADD; border-color: #378ADD; text-decoration: none; font-weight: 600; }
        .button:hover { background: #2870c0; }
        .button[hidden] { display: none; }
        .table-wrap { background: #2a2a2a; border-radius: 8px; overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
        th, td { padding: .75rem 1rem; text-align: right; border-bottom: 1px solid #333; white-space: nowrap; }
        th { font-size: 12px; color: #999; text-transform: uppercase; background: #1f1f1f; }
        th:first-child, td:first-child { text-align: left; }
        tr:last-child td { border-bottom: none; }
        .best { color: #4ade80; font-weight: 600; }
        .slower { color: #f87171; }
        .flag { color: #fbbf24; font-size: 12px; }
        .empty { padding: 2rem; text-align: center; color: #666; }
    </style>
</head>
<body>
<div class="container">
    <h1>🏁 iRacing Telemetry Logger</h1>

    <div class="grid">
        <div class="card"><div class="label">Statut</div><div class="value inactive" id="status">Hors ligne</div></div>
        <div class="card"><div class="label">Circuit</div><div class="value small" id="track">—</div></div>
        <div class="card"><div class="label">Voiture</div><div class="value small" id="car">—</div></div>
        <div class="card"><div class="label">Vitesse</div><div class="value" id="speed">—</div></div>
        <div class="card"><div class="label">Carburant</div><div class="value" id="fuel">—</div></div>
    </div>

    <div class="controls">
        <select id="session-select" aria-label="Session affichée">
            <option value="">Session en cours</option>
        </select>
        <a class="button" id="export" hidden>Exporter en CSV</a>
    </div>

    <div class="grid">
        <div class="card"><div class="label">Tours</div><div class="value" id="laps-count">0</div></div>
        <div class="card"><div class="label">Meilleur tour</div><div class="value" id="best-lap">—</div></div>
        <div class="card"><div class="label">Moyenne (tours propres)</div><div class="value" id="avg-lap">—</div></div>
        <div class="card"><div class="label">Conso moyenne / tour</div><div class="value" id="avg-fuel">—</div></div>
    </div>

    <div class="table-wrap">
        <table>
            <thead>
            <tr>
                <th>Tour</th><th>Temps</th><th>Écart</th><th>Carbu</th>
                <th>V max</th><th>Gaz moy.</th><th>Frein moy.</th><th></th>
            </tr>
            </thead>
            <tbody id="laps"></tbody>
        </table>
        <div class="empty" id="empty">En attente d'un tour complet…</div>
    </div>
</div>

<script>
    const $ = id => document.getElementById(id);
    const FLAGS = { pit: 'stand', partial: 'partiel' };
    let currentSessionId = null;

    function fmtTime(s) {
        if (s == null) return '—';
        const m = Math.floor(s / 60);
        return `${m}:${(s - m * 60).toFixed(3).padStart(6, '0')}`;
    }
    const fmt = (v, digits, unit = '') => v == null ? '—' : `${v.toFixed(digits)}${unit}`;
    const pct = v => v == null ? '—' : `${Math.round(v * 100)} %`;
    const escapeHtml = s => String(s).replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);

    async function getJSON(url) {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`${url} → ${res.status}`);
        return res.json();
    }

    function renderStatus(s) {
        const el = $('status');
        el.textContent = !s.connected ? 'Hors ligne' : s.on_track ? 'En piste' : 'Connecté';
        el.className = 'value ' + (s.connected ? 'active' : 'inactive');
        $('track').textContent = s.track || '—';
        $('car').textContent = s.car || '—';
        $('speed').textContent = s.connected ? fmt(s.speed_kmh, 0, ' km/h') : '—';
        $('fuel').textContent = s.connected ? fmt(s.fuel_l, 1, ' L') : '—';
    }

    async function refreshSessionList() {
        const sessions = await getJSON('/api/sessions');
        const select = $('session-select');
        const selected = select.value;
        select.innerHTML = '<option value="">Session en cours</option>' + sessions.map(s =>
            `<option value="${s.id}">#${s.id} · ${s.started_at.replace('T', ' ')} · ` +
            `${escapeHtml(s.track || '?')} · ${s.laps} tours</option>`).join('');
        select.value = selected;
    }

    function renderLaps(laps, sessionId) {
        const clean = laps.filter(l => l.lap_time != null && !l.flag);
        const best = clean.length ? Math.min(...clean.map(l => l.lap_time)) : null;
        const avg = clean.length ? clean.reduce((a, l) => a + l.lap_time, 0) / clean.length : null;
        const fuelLaps = clean.filter(l => l.fuel_used != null);
        const avgFuel = fuelLaps.length ? fuelLaps.reduce((a, l) => a + l.fuel_used, 0) / fuelLaps.length : null;

        $('laps-count').textContent = laps.length;
        $('best-lap').textContent = fmtTime(best);
        $('avg-lap').textContent = fmtTime(avg);
        $('avg-fuel').textContent = fmt(avgFuel, 2, ' L');

        const exportLink = $('export');
        exportLink.hidden = !sessionId;
        if (sessionId) exportLink.href = `/api/sessions/${sessionId}/export.csv`;

        $('empty').hidden = laps.length > 0;
        $('laps').innerHTML = laps.slice().reverse().map(l => {
            let delta = '';
            if (l.lap_time != null && best != null) {
                const d = l.lap_time - best;
                delta = d === 0 && !l.flag
                    ? '<span class="best">meilleur</span>'
                    : `<span class="${d > 0 ? 'slower' : 'best'}">${d > 0 ? '+' : ''}${d.toFixed(3)}</span>`;
            }
            return `<tr>
                <td>${l.lap_number}</td>
                <td>${fmtTime(l.lap_time)}</td>
                <td>${delta}</td>
                <td>${fmt(l.fuel_used, 2, ' L')}</td>
                <td>${fmt(l.max_speed_kmh, 0, ' km/h')}</td>
                <td>${pct(l.throttle_avg)}</td>
                <td>${pct(l.brake_avg)}</td>
                <td class="flag">${FLAGS[l.flag] || ''}</td>
            </tr>`;
        }).join('');
    }

    async function refresh() {
        try {
            const status = await getJSON('/api/status');
            renderStatus(status);
            if (status.session_id !== currentSessionId) {
                currentSessionId = status.session_id;
                await refreshSessionList();
            }
            const sessionId = $('session-select').value || currentSessionId;
            renderLaps(sessionId ? await getJSON(`/api/sessions/${sessionId}/laps`) : [], sessionId);
        } catch (err) {
            console.error(err);
            $('status').textContent = 'Serveur injoignable';
            $('status').className = 'value inactive';
        }
    }

    $('session-select').addEventListener('change', refresh);
    refreshSessionList().catch(console.error);
    setInterval(refresh, 1000);  // les tours changent au plus une fois par minute, 1 s suffit
    refresh();
</script>
</body>
</html>
```

---

## Lancer le projet

```bash
python main.py
# ou sur un autre port :
python main.py --port 5001
```

Tu dois voir :

```
🏁 iRacing Telemetry Server démarré sur http://localhost:5000
   Lance iRacing et monte en piste !
```

Ensuite :

1. Ouvre `http://localhost:5000` dans ton navigateur.
2. Lance iRacing et une session (test drive, essais, course…).
3. Le statut passe à **Connecté**, puis à **En piste** quand tu es dans la voiture.
4. Chaque tour terminé apparaît dans le tableau environ une seconde après avoir passé la ligne.

Le serveur peut tourner en permanence : il se reconnecte tout seul quand iRacing démarre ou s'arrête.

---

## Données enregistrées par tour

| Champ | Description |
|---|---|
| `lap_number` | Numéro du tour |
| `lap_time` | Temps officiel iRacing (secondes) |
| `fuel_used` / `fuel_left` | Carburant consommé sur le tour / restant à la fin (litres) |
| `max_speed_kmh` | Vitesse maximale sur le tour |
| `throttle_avg` / `brake_avg` | Pression moyenne accélérateur / frein (0 à 1) |
| `flag` | vide = tour propre, `pit` = passage aux stands, `partial` = tour incomplet |
| `recorded_at` | Date et heure d'enregistrement |

Pour ouvrir le CSV dans Excel en français, passe par **Données → À partir d'un fichier texte/CSV** :
le séparateur est la virgule et les décimales utilisent le point.

---

## Limites à connaître

- **Tours uniquement pendant que tu pilotes :** en replay ou en spectateur, rien n'est enregistré (`IsOnTrack` est faux).
- **Températures des pneus :** iRacing ne les met à jour **qu'aux stands**, pas en roulant.
  Les températures de freins et les dégâts ne sont pas exposés du tout.
- **Pas de télémétrie fine :** on stocke un résumé par tour, pas chaque échantillon.
  Pour une analyse point par point, iRacing enregistre déjà ses propres fichiers `.ibt`
  (touche **Alt+L** en piste, dans `Documents\iRacing\telemetry`), lisibles avec `pyirsdk`
  ou des outils comme Garage 61 et MoTeC i2.
- **Autres pilotes :** le SDK donne seulement les tours et positions des voitures de **ta session**
  (`CarIdxLastLapTime`, `CarIdxPosition`…), pas les données d'autres pilotes en dehors.

---

## Idées d'amélioration réalistes

1. **Graphiques** avec [Chart.js](https://www.chartjs.org/) : courbe des temps au tour, consommation par tour.
2. **Secteurs :** découper le tour avec `LapDistPct` (0 → 1) et chronométrer chaque portion
   pour trouver où tu perds du temps.
3. **Plus de données par tour :** `Gear` et `RPM` (rapport le plus utilisé, régime max),
   température de piste (`TrackTempCrew`), etc. La liste complète est dans la doc `pyirsdk`.
4. **Stratégie carburant :** à partir de la conso moyenne, estimer le nombre de tours restants.
5. **Comparaison de sessions :** même circuit et même voiture, meilleur tour et régularité d'une fois sur l'autre.
6. **Gestion des sessions :** renommer ou supprimer une session depuis le dashboard.

---

## Dépannage

| Problème | Solution |
|---|---|
| Statut « Hors ligne » | iRacing n'est pas lancé, ou tu es encore dans l'interface web/launcher. Lance une session. |
| Statut « Connecté » mais aucun tour | Tu n'es pas dans la voiture (garage, replay). Monte en piste et termine un tour complet. |
| Le premier tour est marqué « stand » | Normal : c'est l'out-lap (sortie des stands). |
| `ModuleNotFoundError: irsdk` | `pip install pyirsdk` dans le même environnement virtuel. |
| `TemplateNotFound: index.html` | Le fichier doit être dans le dossier `templates/`. |
| Port 5000 déjà utilisé | `python main.py --port 5001` |

---

## Ressources

- [pyirsdk](https://github.com/kutu/pyirsdk) : bibliothèque Python pour le SDK iRacing (et sa liste des variables)
- Forum iRacing, section *SDK* : documentation officielle et fichiers `irsdk_defines.h`

**Bonne course ! 🏁**
