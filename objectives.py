"""Gamification : objectifs par voiture et par voiture × circuit, expérience et niveaux.

Les objectifs sont des *modèles* définis ici. La première fois qu'une voiture (ou un circuit
pour une voiture) apparaît, tous les modèles sont copiés dans la base pour elle : chaque
voiture a donc sa propre progression. Les nouveaux modèles ajoutés plus tard sont ajoutés
automatiquement aux voitures existantes.

Les objectifs de temps au tour sont toujours **relatifs à ta propre référence** sur ce
couple voiture × circuit (le meilleur de tes 3 premiers tours propres) : jamais un temps
absolu irréaliste.
"""
import json
import statistics
import threading

import analysis
import db

XP_BY_TIER = {"bronze": 50, "argent": 100, "or": 200, "platine": 400}
BASELINE_LAPS = 3  # tours propres utilisés pour établir la référence d'un circuit

LEVEL_TITLES = [
    (1, "Rookie"), (5, "Licence D"), (10, "Licence C"), (15, "Licence B"),
    (22, "Licence A"), (30, "Pro"), (40, "Pro/WC"), (50, "Légende"),
]


def _obj(code, category, title, metric, target, tier, compare="gte", unit=""):
    return {"code": code, "category": category, "title": title, "metric": metric,
            "target": target, "tier": tier, "compare": compare, "unit": unit}


def _series(prefix, category, title, metric, steps, unit="", compare="gte"):
    """Une série d'objectifs de difficulté croissante : bronze → platine."""
    names = ["bronze", "argent", "or", "platine"]
    tiers = [names[i * len(names) // len(steps)] for i in range(len(steps))]
    return [_obj(f"{prefix}_{str(step).replace('.', '_')}", category, _title(title, step), metric, step, tier, compare, unit)
            for step, tier in zip(steps, tiers)]


def _title(template, n):
    text = template.format(n=str(n).replace(".", ","))  # 0.15 -> 0,15
    if n == 1:
        for plural, singular in (("tours", "tour"), ("sessions", "session"), ("circuits", "circuit"), ("courses", "course")):
            text = text.replace(f"1 {plural}", f"1 {singular}")
    return text


# --- modèles d'objectifs par voiture ---------------------------------------------------------

CAR_TEMPLATES = (
    _series("laps", "Volume", "Boucler {n} tours", "laps", [1, 10, 25, 50, 100, 250, 500, 1000], "tours")
    + _series("clean", "Volume", "{n} tours propres", "clean_laps", [5, 25, 100, 250, 500, 1000], "tours")
    + _series("km", "Distance", "Parcourir {n} km", "distance_km", [10, 50, 100, 250, 500, 1000, 2500, 5000], "km")
    + _series("hours", "Distance", "{n} h de roulage", "hours", [1, 3, 5, 10, 25, 50], "h")
    + _series("sessions", "Assiduité", "{n} sessions", "sessions", [1, 5, 10, 25, 50, 100], "sessions")
    + _series("tracks", "Découverte", "Rouler sur {n} circuits", "tracks", [1, 3, 5, 10, 15, 20], "circuits")
    + _series("races", "Compétition", "Participer à {n} courses", "race_sessions", [1, 5, 10, 25, 50], "courses")
    + _series("streak", "Régularité", "{n} tours propres d'affilée", "best_clean_streak", [3, 5, 10, 15, 20, 30], "tours")
    + _series("longrun", "Endurance", "Session de {n} tours propres", "max_clean_laps_session", [10, 20, 30, 45, 60], "tours")
    + _series("stdev", "Régularité", "Écart-type sous {n} s sur une session (5 tours propres min.)",
              "best_session_stdev", [1.0, 0.6, 0.4, 0.3, 0.2, 0.15], "s", compare="lte")
    + _series("fuel", "Endurance", "Brûler {n} L de carburant", "fuel_l", [50, 200, 500, 1000, 2500], "L")
    + _series("pbtracks", "Découverte", "Améliorer ta référence de 1 % sur {n} circuits",
              "tracks_improved_1pct", [1, 3, 5, 10], "circuits")
)

# --- modèles d'objectifs par voiture × circuit -----------------------------------------------

TRACK_TEMPLATES = (
    _series("t_laps", "Volume", "Boucler {n} tours sur ce circuit", "laps", [5, 10, 25, 50, 100, 200], "tours")
    + _series("t_clean", "Volume", "{n} tours propres sur ce circuit", "clean_laps", [5, 20, 50, 100], "tours")
    + _series("t_sessions", "Assiduité", "{n} sessions sur ce circuit", "sessions", [2, 5, 10, 20], "sessions")
    + _series("t_km", "Distance", "{n} km sur ce circuit", "distance_km", [50, 100, 250, 500], "km")
    + _series("t_pb", "Chrono", "Battre ta référence de {n} %", "improvement_pct",
              [0.5, 1, 1.5, 2, 3, 4, 5, 6], "%")
    + _series("t_near1", "Régularité", "{n} tours à moins de 1 % de ton record", "laps_within_1pct", [5, 20, 50], "tours")
    + _series("t_near05", "Régularité", "{n} tours à moins de 0,5 % de ton record", "laps_within_05pct", [3, 10, 25], "tours")
    + _series("t_stdev", "Régularité", "Écart-type sous {n} s sur une session (5 tours propres min.)",
              "best_session_stdev", [0.8, 0.5, 0.3, 0.2], "s", compare="lte")
    + _series("t_ideal", "Chrono", "Meilleur tour à moins de {n} s du tour idéal (session de 5 tours propres min.)",
              "best_ideal_gap", [0.5, 0.3, 0.15, 0.08], "s", compare="lte")
    + _series("t_avg", "Chrono", "Moyenne d'une session à moins de {n} % de ton record (5 tours propres min.)",
              "best_avg_gap_pct", [2, 1, 0.6, 0.4], "%", compare="lte")
)


# --- niveaux -------------------------------------------------------------------------------

def xp_for_next(level):
    """XP nécessaire pour passer du niveau `level` au suivant : 200, 300, 400…"""
    return 200 + 100 * (level - 1)


def level_from_xp(xp):
    level, remaining = 1, xp
    while remaining >= xp_for_next(level):
        remaining -= xp_for_next(level)
        level += 1
    title = next(t for lvl, t in reversed(LEVEL_TITLES) if level >= lvl)
    return {"xp": xp, "level": level, "title": title,
            "level_xp": remaining, "next_level_xp": xp_for_next(level)}


# --- métriques -----------------------------------------------------------------------------

def _laps_of(car, track=None):
    sql = """SELECT l.*, s.track, s.track_length_m, s.session_type
             FROM laps l JOIN sessions s ON s.id = l.session_id
             WHERE s.car = ?"""
    params = [car]
    if track is not None:
        sql += " AND s.track = ?"
        params.append(track)
    laps = db.query(sql + " ORDER BY l.session_id, l.lap_number, l.id", params)
    for lap in laps:
        lap["sectors"] = json.loads(lap["sectors"]) if lap["sectors"] else None
    return laps


def _is_clean(lap):
    return lap["flag"] is None and lap["lap_time"] is not None


def _by_session(laps):
    sessions = {}
    for lap in laps:
        sessions.setdefault(lap["session_id"], []).append(lap)
    return sessions


def _common_metrics(laps):
    sessions = _by_session(laps)
    clean = [l for l in laps if _is_clean(l)]
    best_streak, max_session_clean, stdevs = 0, 0, []
    for session_laps in sessions.values():
        streak = 0
        for lap in session_laps:
            streak = streak + 1 if _is_clean(lap) else 0
            best_streak = max(best_streak, streak)
        times = [l["lap_time"] for l in session_laps if _is_clean(l)]
        max_session_clean = max(max_session_clean, len(times))
        if len(times) >= 5:
            stdevs.append(statistics.stdev(times))
    return {
        "laps": len(laps),
        "clean_laps": len(clean),
        "sessions": len(sessions),
        "distance_km": sum((l["track_length_m"] or 0) for l in laps) / 1000,
        "hours": sum(l["lap_time"] for l in laps if l["lap_time"]) / 3600,
        "fuel_l": sum(l["fuel_used"] or 0 for l in laps),
        "best_clean_streak": best_streak,
        "max_clean_laps_session": max_session_clean,
        "best_session_stdev": min(stdevs) if stdevs else None,
    }


def track_metrics(car, track):
    laps = _laps_of(car, track)
    metrics = _common_metrics(laps)
    clean = [l for l in laps if _is_clean(l)]
    baseline = min(l["lap_time"] for l in clean[:BASELINE_LAPS]) if len(clean) >= BASELINE_LAPS else None
    pb = min((l["lap_time"] for l in clean), default=None)
    metrics.update({
        "baseline": baseline,
        "pb": pb,
        "improvement_pct": (baseline - pb) / baseline * 100 if baseline else None,
        "laps_within_1pct": sum(1 for l in clean if pb and l["lap_time"] <= pb * 1.01),
        "laps_within_05pct": sum(1 for l in clean if pb and l["lap_time"] <= pb * 1.005),
        "best_ideal_gap": None,
        "best_avg_gap_pct": None,
    })
    ideal_gaps, avg_gaps = [], []
    for session_laps in _by_session(laps).values():
        stats = analysis.session_stats(session_laps)
        if stats["ideal_lap"] is not None and stats["clean_laps"] >= 5:
            ideal_gaps.append(stats["best_lap"] - stats["ideal_lap"])
        if stats["clean_laps"] >= 5 and pb:
            avg_gaps.append((stats["avg_lap"] - pb) / pb * 100)
    metrics["best_ideal_gap"] = min(ideal_gaps) if ideal_gaps else None
    metrics["best_avg_gap_pct"] = min(avg_gaps) if avg_gaps else None
    return metrics


def car_metrics(car):
    laps = _laps_of(car)
    metrics = _common_metrics(laps)
    tracks = sorted({l["track"] for l in laps if l["track"]})
    metrics["tracks"] = len(tracks)
    metrics["race_sessions"] = len({l["session_id"] for l in laps if "race" in (l["session_type"] or "").lower()})
    metrics["tracks_improved_1pct"] = sum(
        1 for t in tracks if (track_metrics(car, t)["improvement_pct"] or 0) >= 1)
    return metrics


# --- base : réplication et évaluation --------------------------------------------------------

_lock = threading.Lock()  # la capture et le serveur peuvent évaluer en même temps


def ensure_car(car, track=None):
    """Crée la voiture (et le circuit) si inconnus, en répliquant tous les modèles d'objectifs."""
    if not car:
        return
    rows = [(car, "", t["code"], "car", XP_BY_TIER[t["tier"]]) for t in CAR_TEMPLATES]
    if track:
        rows += [(car, track, t["code"], "track", XP_BY_TIER[t["tier"]]) for t in TRACK_TEMPLATES]
    conn = db.connect()
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO cars (name, created_at) VALUES (?, ?)", (car, db.now()))
            if track:
                conn.execute("INSERT OR IGNORE INTO car_tracks (car, track, created_at) VALUES (?, ?, ?)",
                             (car, track, db.now()))
            conn.executemany(
                "INSERT OR IGNORE INTO objectives (car, track, code, scope, xp) VALUES (?, ?, ?, ?, ?)", rows)
    finally:
        conn.close()


def _met(template, value):
    if value is None:
        return False
    return value >= template["target"] if template["compare"] == "gte" else value <= template["target"]


def _progress(template, value):
    """Avancement 0..1 pour la barre de progression."""
    if value is None:
        return 0.0
    if template["compare"] == "gte":
        return max(0.0, min(1.0, value / template["target"]))
    return max(0.0, min(1.0, template["target"] / value)) if value > 0 else 1.0


def _evaluate(car, track, templates, metrics, conn):
    unlocked = []
    rows = conn.execute("SELECT id, code, completed_at, xp FROM objectives WHERE car = ? AND track = ?",
                        (car, track)).fetchall()
    by_code = {t["code"]: t for t in templates}
    for row in rows:
        template = by_code.get(row["code"])
        if not template:
            continue
        value = metrics.get(template["metric"])
        conn.execute("UPDATE objectives SET value = ? WHERE id = ?", (value, row["id"]))
        if row["completed_at"] is None and _met(template, value):
            conn.execute("UPDATE objectives SET completed_at = ? WHERE id = ?", (db.now(), row["id"]))
            conn.execute("INSERT INTO xp_events (objective_id, xp, at) VALUES (?, ?, ?)",
                         (row["id"], row["xp"], db.now()))
            unlocked.append(template["title"])
    return unlocked


def evaluate(car, track=None):
    """Recalcule la progression d'une voiture (et d'un circuit) ; renvoie les objectifs débloqués."""
    if not car:
        return []
    with _lock:
        ensure_car(car, track)
        car_m = car_metrics(car)
        track_m = track_metrics(car, track) if track else None
        conn = db.connect()
        try:
            with conn:
                unlocked = _evaluate(car, "", CAR_TEMPLATES, car_m, conn)
                if track:
                    unlocked += _evaluate(car, track, TRACK_TEMPLATES, track_m, conn)
        finally:
            conn.close()
    for title in unlocked:
        print(f"[objectifs] 🏆 {car}{' / ' + track if track else ''} : {title}")
    return unlocked


def evaluate_all():
    """Au démarrage : crée les entrées manquantes et rattrape les objectifs des sessions existantes."""
    for row in db.query("SELECT DISTINCT car, track FROM sessions WHERE car IS NOT NULL"):
        evaluate(row["car"], row["track"])


# --- lecture pour l'interface ------------------------------------------------------------------

def profile():
    xp = db.query_one("SELECT COALESCE(SUM(xp), 0) AS xp FROM xp_events")["xp"]
    counts = db.query_one("""SELECT COUNT(*) AS total, COUNT(completed_at) AS completed FROM objectives""")
    return {**level_from_xp(xp), **counts}


def _format_objective(template, row, metrics=None):
    item = {
        "code": template["code"], "category": template["category"], "title": template["title"],
        "tier": template["tier"], "xp": row["xp"], "unit": template["unit"],
        "target": template["target"], "value": row["value"], "compare": template["compare"],
        "completed_at": row["completed_at"],
        "progress": 1.0 if row["completed_at"] else _progress(template, row["value"]),
        "hint": None,
    }
    if template["metric"] == "improvement_pct" and metrics is not None:
        # traduit le pourcentage en un vrai temps cible, basé sur ta référence
        if metrics["baseline"]:
            target_time = metrics["baseline"] * (1 - template["target"] / 100)
            item["target_time"] = target_time
            item["hint"] = "ref"
        else:
            item["hint"] = "baseline"
    return item


def list_objectives(car, track=None):
    templates = TRACK_TEMPLATES if track else CAR_TEMPLATES
    rows = {r["code"]: r for r in db.query(
        "SELECT * FROM objectives WHERE car = ? AND track = ?", (car, track or ""))}
    metrics = track_metrics(car, track) if track else None
    items = [_format_objective(t, rows[t["code"]], metrics) for t in templates if t["code"] in rows]
    return {"objectives": items,
            "baseline": metrics["baseline"] if metrics else None,
            "pb": metrics["pb"] if metrics else None}


def recent_unlocks(after_id=0):
    rows = db.query("""SELECT e.id, e.xp, e.at, o.car, o.track, o.code FROM xp_events e
                       JOIN objectives o ON o.id = e.objective_id
                       WHERE e.id > ? ORDER BY e.id""", (after_id,))
    titles = {t["code"]: t["title"] for t in CAR_TEMPLATES + TRACK_TEMPLATES}
    for row in rows:
        row["title"] = titles.get(row["code"], row["code"])
    return rows
