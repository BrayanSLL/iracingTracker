"""Base SQLite : sessions, tours et traces de télémétrie (60 Hz) de chaque tour."""
import json
import sqlite3
import zlib
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "sessions.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    track TEXT,
    track_length_m REAL,
    car TEXT,
    session_type TEXT
);
CREATE TABLE IF NOT EXISTS laps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    lap_number INTEGER NOT NULL,
    lap_time REAL,          -- secondes, NULL si iRacing ne donne pas de temps valide
    sectors TEXT,           -- JSON : temps des secteurs (secondes)
    fuel_used REAL,         -- litres consommés sur le tour
    fuel_left REAL,         -- litres restants à la fin du tour
    max_speed_kmh REAL,
    throttle_avg REAL,      -- 0..1
    brake_avg REAL,         -- 0..1
    flag TEXT,              -- NULL = tour propre, 'pit' ou 'partial'
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS traces (
    lap_id INTEGER PRIMARY KEY REFERENCES laps(id) ON DELETE CASCADE,
    data BLOB NOT NULL      -- JSON compressé (zlib) : une liste par canal
);
CREATE INDEX IF NOT EXISTS laps_session ON laps(session_id);

-- Gamification : une entrée par voiture (et voiture × circuit), objectifs répliqués depuis objectives.py
CREATE TABLE IF NOT EXISTS cars (
    name TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS car_tracks (
    car TEXT NOT NULL REFERENCES cars(name),
    track TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (car, track)
);
CREATE TABLE IF NOT EXISTS objectives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    car TEXT NOT NULL REFERENCES cars(name),
    track TEXT NOT NULL DEFAULT '',   -- '' = objectif de la voiture, sinon objectif de circuit
    code TEXT NOT NULL,               -- identifiant du modèle dans objectives.py
    scope TEXT NOT NULL,              -- 'car' ou 'track'
    xp INTEGER NOT NULL,
    value REAL,                       -- dernière valeur mesurée
    completed_at TEXT,                -- NULL tant que l'objectif n'est pas réussi
    UNIQUE (car, track, code)
);
-- Habitudes de pilotage de chaque session (pour dire « 3e session d'affilée » ou « corrigé »)
CREATE TABLE IF NOT EXISTS habit_runs (
    session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    laps INTEGER NOT NULL,            -- tours analysés
    computed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_habits (
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    corner INTEGER,                   -- NULL = erreur sur tout le tour (passages de rapport…)
    code TEXT NOT NULL,               -- type d'erreur (technique.py)
    count INTEGER NOT NULL            -- nombre de tours où elle apparaît
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS xp_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    objective_id INTEGER NOT NULL REFERENCES objectives(id),
    xp INTEGER NOT NULL,
    at TEXT NOT NULL
);
"""


def connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # nécessaire pour ON DELETE CASCADE
    return conn


def execute(sql, params=()):
    """Exécute une écriture et renvoie l'id de la ligne insérée."""
    conn = connect()
    try:
        with conn:
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


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


# Colonnes ajoutées après la première version : ajoutées aux bases existantes au démarrage.
MIGRATIONS = {
    "sessions": {"name": "TEXT", "note": "TEXT", "shift_rpm": "REAL", "redline_rpm": "REAL"},
}


def init_db():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, kind in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        conn.commit()
    finally:
        conn.close()


def now():
    return datetime.now().isoformat(timespec="seconds")


# --- sessions -----------------------------------------------------------------

def create_session(track, track_length_m, car, session_type, shift_rpm=None, redline_rpm=None):
    return execute(
        """INSERT INTO sessions (started_at, track, track_length_m, car, session_type, shift_rpm, redline_rpm)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (now(), track, track_length_m, car, session_type, shift_rpm, redline_rpm),
    )


def delete_session(session_id):
    execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn = connect()
    try:
        conn.execute("VACUUM")  # rend réellement la place sur le disque
    finally:
        conn.close()


def list_sessions():
    return query("""
        SELECT s.*,
               COUNT(l.id) AS laps,
               MIN(CASE WHEN l.flag IS NULL THEN l.lap_time END) AS best_lap
        FROM sessions s
        LEFT JOIN laps l ON l.session_id = s.id
        GROUP BY s.id
        ORDER BY s.id DESC
    """)


def update_session(session_id, name, note):
    execute("UPDATE sessions SET name = ?, note = ? WHERE id = ?", (name or None, note or None, session_id))


def is_race(session):
    return "race" in (session.get("session_type") or "").lower()


# --- sauvegarde / restauration ----------------------------------------------------

REQUIRED_TABLES = {"sessions", "laps", "traces"}


def backup_to(path):
    """Copie cohérente de la base (même pendant un enregistrement) via l'API de sauvegarde SQLite."""
    src, dst = connect(), sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def restore_from(path):
    """Remplace le contenu de la base par celui d'une sauvegarde, après l'avoir vérifiée.

    Une copie de la base actuelle est d'abord gardée dans data/ (au cas où).
    """
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        try:
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            healthy = src.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        except sqlite3.DatabaseError as exc:
            raise ValueError("ce fichier n'est pas une sauvegarde valide") from exc
        if not healthy:
            raise ValueError("fichier de sauvegarde corrompu")
        if not REQUIRED_TABLES <= tables:
            raise ValueError("ce fichier n'est pas une sauvegarde de l'application")
        safety = DB_PATH.parent / f"sessions-avant-restauration-{datetime.now():%Y%m%d-%H%M%S}.db"
        backup_to(safety)
        dst = connect()
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    init_db()  # met à niveau une sauvegarde ancienne
    return safety.name


# --- tours --------------------------------------------------------------------

def insert_lap(session_id, lap, trace):
    conn = connect()
    try:
        with conn:
            lap_id = conn.execute(
                """INSERT INTO laps (session_id, lap_number, lap_time, sectors, fuel_used, fuel_left,
                                     max_speed_kmh, throttle_avg, brake_avg, flag, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, lap["lap_number"], lap["lap_time"], json.dumps(lap["sectors"]),
                 lap["fuel_used"], lap["fuel_left"], lap["max_speed_kmh"], lap["throttle_avg"],
                 lap["brake_avg"], lap["flag"], now()),
            ).lastrowid
            if trace:
                conn.execute("INSERT INTO traces (lap_id, data) VALUES (?, ?)",
                             (lap_id, zlib.compress(json.dumps(trace).encode())))
            return lap_id
    finally:
        conn.close()


def list_laps(session_id):
    laps = query("""
        SELECT l.*, t.lap_id IS NOT NULL AS has_trace
        FROM laps l LEFT JOIN traces t ON t.lap_id = l.id
        WHERE l.session_id = ?
        ORDER BY l.lap_number, l.id
    """, (session_id,))
    for lap in laps:
        lap["sectors"] = json.loads(lap["sectors"]) if lap["sectors"] else None
        lap["has_trace"] = bool(lap["has_trace"])
    return laps


def get_lap(lap_id):
    return query_one("SELECT * FROM laps WHERE id = ?", (lap_id,))


def get_trace(lap_id):
    row = query_one("SELECT data FROM traces WHERE lap_id = ?", (lap_id,))
    return json.loads(zlib.decompress(row["data"])) if row else None


def delete_lap(lap_id):
    execute("DELETE FROM laps WHERE id = ?", (lap_id,))


def record_lap(car, track, exclude_id=None):
    """Meilleur tour propre (avec télémétrie) de cette voiture sur ce circuit, toutes sessions confondues."""
    return query_one("""
        SELECT l.id, l.lap_time, l.lap_number, l.session_id, l.sectors, s.started_at, s.name AS session_name
        FROM laps l
        JOIN sessions s ON s.id = l.session_id
        JOIN traces t ON t.lap_id = l.id
        WHERE s.car IS ? AND s.track IS ? AND l.flag IS NULL AND l.lap_time IS NOT NULL AND l.id IS NOT ?
        ORDER BY l.lap_time LIMIT 1
    """, (car, track, exclude_id))


def lap_meta(lap_id):
    row = query_one("""
        SELECT l.id, l.lap_number, l.lap_time, l.sectors, l.session_id, s.started_at, s.name AS session_name,
               s.car, s.track, s.track_length_m, s.shift_rpm, s.redline_rpm
        FROM laps l JOIN sessions s ON s.id = l.session_id WHERE l.id = ?
    """, (lap_id,))
    if row:
        row["sectors"] = json.loads(row["sectors"]) if row["sectors"] else None
    return row


def record_history(car, track):
    """Meilleur tour propre de chaque session et évolution du record, dans l'ordre chronologique."""
    rows = query("""
        SELECT s.id AS session_id, s.started_at, s.name, MIN(l.lap_time) AS session_best
        FROM sessions s JOIN laps l ON l.session_id = s.id
        WHERE s.car IS ? AND s.track IS ? AND l.flag IS NULL AND l.lap_time IS NOT NULL
        GROUP BY s.id ORDER BY s.started_at, s.id
    """, (car, track))
    record = None
    for row in rows:
        record = row["session_best"] if record is None else min(record, row["session_best"])
        row["record"] = record
    return rows


# --- réglages -------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "voice": "records",   # off | records | laps (chaque tour) ; le mode entraînement parle toujours si voice != off
    "voice_rate": "normal",   # lent | normal | rapide
    "voice_volume": "100",    # 0 à 100 (100 = maximum de la synthèse vocale Windows)
    "overlay": "on",          # on | off : fenêtre du delta en direct (pris en compte au prochain lancement)
}


def get_setting(key):
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else DEFAULT_SETTINGS.get(key)


def set_setting(key, value):
    execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))


# --- habitudes de pilotage ----------------------------------------------------------------

def save_habits(session_id, laps_analysed, habits):
    """habits = [(virage ou None, code, nombre de tours)]"""
    conn = connect()
    try:
        with conn:
            conn.execute("DELETE FROM session_habits WHERE session_id = ?", (session_id,))
            conn.executemany("INSERT INTO session_habits (session_id, corner, code, count) VALUES (?, ?, ?, ?)",
                             [(session_id, corner, code, count) for corner, code, count in habits])
            conn.execute("INSERT INTO habit_runs (session_id, laps, computed_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(session_id) DO UPDATE SET laps = excluded.laps, computed_at = excluded.computed_at",
                         (session_id, laps_analysed, now()))
    finally:
        conn.close()


def previous_sessions(session, limit=5, analysed_only=True):
    """Sessions précédentes sur le même couple voiture × circuit, de la plus récente à la plus ancienne."""
    join = "JOIN habit_runs r ON r.session_id = s.id" if analysed_only else ""
    return query(f"""
        SELECT s.* FROM sessions s {join}
        WHERE s.car IS ? AND s.track IS ? AND s.id != ? AND (s.started_at < ? OR (s.started_at = ? AND s.id < ?))
        ORDER BY s.started_at DESC, s.id DESC LIMIT ?
    """, (session["car"], session["track"], session["id"], session["started_at"], session["started_at"],
          session["id"], limit))


def habits_of(session_id):
    return {(r["corner"], r["code"]): r["count"]
            for r in query("SELECT corner, code, count FROM session_habits WHERE session_id = ?", (session_id,))}


def has_habit_run(session_id):
    return query_one("SELECT 1 AS ok FROM habit_runs WHERE session_id = ?", (session_id,)) is not None
