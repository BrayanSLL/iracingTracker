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


def init_db():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def now():
    return datetime.now().isoformat(timespec="seconds")


# --- sessions -----------------------------------------------------------------

def create_session(track, track_length_m, car, session_type):
    return execute(
        "INSERT INTO sessions (started_at, track, track_length_m, car, session_type) VALUES (?, ?, ?, ?, ?)",
        (now(), track, track_length_m, car, session_type),
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
