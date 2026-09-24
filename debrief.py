"""Débrief automatique d'une session : points forts, points à améliorer et priorité.

Tout est calculé à partir des temps au tour, des secteurs et de la télémétrie des virages.
Les remarques « à améliorer » ont un poids (≈ temps gagnable par tour, en secondes) qui sert
à choisir la priorité pour la prochaine session.
"""
import statistics

import analysis
import db

MAX_LAPS_ANALYSED = 15      # tours comparés virage par virage (les plus récents)
DEBRIEF_POINTS = 2000       # grille plus grossière que l'affichage : suffisant et bien plus rapide
_cache = {}


def _fmt_time(seconds):
    ms = round(seconds * 1000)
    return f"{ms // 60000}:{(ms % 60000) / 1000:06.3f}"


def _s(value, digits=2):
    return f"{value:.{digits}f}".replace(".", ",")


def _remark(title, detail="", weight=0.0):
    return {"title": title, "detail": detail, "weight": weight}


def session_debrief(session_id):
    """Débrief mis en cache tant que la session ne change pas (nouveau tour, tour supprimé…)."""
    laps = db.list_laps(session_id)
    key = (session_id, len(laps), laps[-1]["id"] if laps else None)
    if key not in _cache:
        if len(_cache) > 50:
            _cache.clear()
        _cache[key] = _build(session_id, laps)
    return _cache[key]


def _build(session_id, laps):
    session = db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
    stats = analysis.session_stats(laps)
    clean = [l for l in laps if l["flag"] is None and l["lap_time"] is not None]
    good, bad = [], []

    if len(clean) < 3:
        return {"ready": False, "good": [], "bad": [], "priority": None,
                "message": f"Pas encore assez de tours propres pour un débrief ({len(clean)}/3)."}

    _record(session, stats, good, bad)
    _consistency(clean, stats, good, bad)
    _ideal_lap(stats, good, bad)
    _sectors(clean, good, bad)
    _trend(clean, good, bad)
    _validity(laps, clean, good, bad)
    _corners(session, clean, stats, good, bad)

    priority = max(bad, key=lambda r: r["weight"]) if bad else None
    if priority and priority["weight"] <= 0:
        priority = None
    return {"ready": True, "good": good, "bad": sorted(bad, key=lambda r: -r["weight"]),
            "priority": priority, "message": None}


# --- règles sur les temps --------------------------------------------------------------------

def _record(session, stats, good, bad):
    previous = db.query_one("""
        SELECT MIN(l.lap_time) AS best FROM laps l JOIN sessions s ON s.id = l.session_id
        WHERE s.car IS ? AND s.track IS ? AND s.id != ? AND s.started_at <= ?
          AND l.flag IS NULL AND l.lap_time IS NOT NULL
    """, (session["car"], session["track"], session["id"], session["started_at"]))["best"]
    best = stats["best_lap"]
    if previous is None:
        good.append(_remark("Première référence sur ce circuit",
                            f"Ton meilleur tour ({_fmt_time(best)}) devient ta référence avec cette voiture."))
    elif best < previous:
        good.append(_remark(f"Nouveau record : {_fmt_time(best)}",
                            f"{_s(previous - best, 3)} s de mieux que ton ancien record ({_fmt_time(previous)})."))
    elif (best - previous) / previous > 0.01:
        bad.append(_remark(f"À {_s(best - previous)} s de ton record",
                           f"Ton record est {_fmt_time(previous)}. Conditions, setup ou rythme ? "
                           "Compare ce tour à ton record dans la télémétrie."))


def _consistency(clean, stats, good, bad):
    if len(clean) < 5 or stats["stdev"] is None:
        return
    stdev = stats["stdev"]
    if stdev <= 0.3:
        good.append(_remark(f"Très régulier : ± {_s(stdev, 3)} s",
                            f"Sur {len(clean)} tours propres. C'est la base pour aller chercher du temps."))
    elif stdev >= 0.8:
        far = sum(1 for l in clean if l["lap_time"] - stats["best_lap"] > 1.0)
        bad.append(_remark(f"Rythme irrégulier : ± {_s(stdev, 3)} s",
                           f"{far} tour(s) à plus d'une seconde de ton meilleur tour. "
                           "Vise d'abord des tours propres et identiques avant d'attaquer.",
                           weight=stdev * 0.5))


def _ideal_lap(stats, good, bad):
    if stats["ideal_lap"] is None or stats["clean_laps"] < 3:
        return
    gap = stats["best_lap"] - stats["ideal_lap"]
    if gap <= 0.15:
        good.append(_remark("Tour quasi parfait",
                            f"Ton meilleur tour n'est qu'à {_s(gap, 3)} s de ton tour idéal ({_fmt_time(stats['ideal_lap'])})."))
    elif gap >= 0.5:
        bad.append(_remark(f"{_s(gap)} s de potentiel non exploité",
                           f"Ton tour idéal ({_fmt_time(stats['ideal_lap'])}) combine tes meilleurs secteurs, "
                           "mais ils ne sont jamais sur le même tour.", weight=gap * 0.6))


def _sectors(clean, good, bad):
    rows = [l["sectors"] for l in clean if l["sectors"] and len(l["sectors"]) == analysis.SECTOR_COUNT]
    if len(rows) < 5:
        return
    spreads = [statistics.stdev(col) for col in zip(*rows)]
    worst = max(range(len(spreads)), key=lambda i: spreads[i])
    best = min(range(len(spreads)), key=lambda i: spreads[i])
    if spreads[worst] >= 0.15:
        bad.append(_remark(f"Secteur S{worst + 1} irrégulier (± {_s(spreads[worst], 3)} s)",
                           "C'est la portion du circuit où tes temps varient le plus d'un tour à l'autre.",
                           weight=spreads[worst]))
    if spreads[best] <= 0.05:
        good.append(_remark(f"Secteur S{best + 1} maîtrisé (± {_s(spreads[best], 3)} s)",
                            "Tu y passes quasiment au même temps à chaque tour."))


def _trend(clean, good, bad):
    if len(clean) < 6:
        return
    third = len(clean) // 3
    start = statistics.mean(l["lap_time"] for l in clean[:third])
    end = statistics.mean(l["lap_time"] for l in clean[-third:])
    if start - end >= 0.3:
        good.append(_remark("Progression pendant la session",
                            f"Tes derniers tours sont en moyenne {_s(start - end)} s plus rapides que les premiers."))
    elif end - start >= 0.4:
        bad.append(_remark(f"Fin de session plus lente (+{_s(end - start)} s)",
                           "Usure des pneus, carburant, piste qui évolue ou fatigue ? "
                           "Compare un tour de début et un tour de fin.", weight=0.15))


def _validity(laps, clean, good, bad):
    invalid = [l for l in laps if l["flag"] is None and l["lap_time"] is None]
    streak = best_streak = 0
    for lap in laps:
        streak = streak + 1 if lap["flag"] is None and lap["lap_time"] is not None else 0
        best_streak = max(best_streak, streak)
    if invalid:
        bad.append(_remark(f"{len(invalid)} tour(s) sans temps valide",
                           "iRacing n'a pas validé ces tours (sortie de piste, coupe…). "
                           "Chaque tour invalidé est un tour perdu pour progresser.", weight=0.05 * len(invalid)))
    elif len(clean) >= 5:
        good.append(_remark("Aucun tour invalidé", "Tous tes tours complets ont un temps valide."))
    if best_streak >= 10:
        good.append(_remark(f"{best_streak} tours propres d'affilée", "Belle concentration sur la durée."))


# --- règles sur la télémétrie des virages ----------------------------------------------------

def _corners(session, clean, stats, good, bad):
    best_id = stats["best_lap_id"]
    best_trace = db.get_trace(best_id) if best_id else None
    others = [l for l in clean if l["id"] != best_id and l["has_trace"]][-MAX_LAPS_ANALYSED:]
    if not best_trace or len(others) < 2:
        return
    length = session["track_length_m"]
    per_corner = {}
    for lap in others:
        trace = db.get_trace(lap["id"])
        if not trace:
            continue
        result = analysis.compare(trace, best_trace, points=DEBRIEF_POINTS)
        for corner in analysis.corner_analysis(result, length):
            per_corner.setdefault(corner["number"], []).append(corner)
    if not per_corner:
        return

    summary = []
    for number, items in per_corner.items():
        losses = [c["time_lost"] for c in items]
        brakes = [c["lap"]["brake_point"] for c in items if c["lap"]["brake_point"] is not None]
        summary.append({
            "number": number,
            "loss": statistics.mean(losses),
            "brake_spread": statistics.stdev(brakes) if len(brakes) >= 2 else None,
            "coast": statistics.mean(c["lap"]["coast"] for c in items),
            "ref": items[0]["ref"],
            "avg": _average_values(items),
        })

    unit = "m" if length else "%"
    for corner in sorted(summary, key=lambda c: -c["loss"])[:2]:
        if corner["loss"] >= 0.08:
            tips = analysis._advice(corner["avg"], corner["ref"], unit)
            bad.append(_remark(f"Virage {corner['number']} : −{_s(corner['loss'])} s par tour",
                               "En moyenne par rapport à ton meilleur tour. "
                               + ("Le plus souvent : " + ", ".join(t[0].lower() + t[1:] for t in tips) + "."
                                  if tips else "Regarde ce virage dans la télémétrie."),
                               weight=corner["loss"]))
    steady = min(summary, key=lambda c: c["loss"])
    if steady["loss"] <= 0.03:
        detail = (f"Tu n'y perds en moyenne que {_s(steady['loss'], 3)} s par rapport à ton meilleur tour."
                  if steady["loss"] > 0.005 else "Tu y es en moyenne aussi rapide que sur ton meilleur tour.")
        good.append(_remark(f"Virage {steady['number']} maîtrisé", detail))

    spreads = [c for c in summary if c["brake_spread"] is not None]
    if spreads and length:
        worst = max(spreads, key=lambda c: c["brake_spread"])
        overall = statistics.mean(c["brake_spread"] for c in spreads)
        if worst["brake_spread"] >= 15:
            bad.append(_remark(f"Freinage variable au virage {worst['number']} (± {worst['brake_spread']:.0f} m)",
                               "Choisis un repère visuel fixe (panneau, marque au sol) et freine toujours au même endroit.",
                               weight=0.1))
        elif overall <= 6:
            good.append(_remark(f"Points de freinage réguliers (± {overall:.0f} m en moyenne)",
                                "Tu freines au même endroit d'un tour à l'autre."))

    coast = sum(c["coast"] for c in summary)
    best_coast = sum(c["ref"]["coast"] for c in summary)
    if coast - best_coast >= 0.2:
        bad.append(_remark(f"Trop de roue libre : +{_s(coast - best_coast, 1)} s par tour",
                           "Moments sans gaz ni frein, en plus de ton meilleur tour. "
                           "Enchaîne plus directement frein → gaz.", weight=(coast - best_coast) * 0.5))


def _average_values(items):
    """Moyenne des mesures d'un virage sur plusieurs tours (pour générer un conseil représentatif)."""
    keys = ("brake_point", "brake_max", "min_speed", "throttle_point", "coast")
    avg = {}
    for key in keys:
        values = [c["lap"][key] for c in items if c["lap"][key] is not None]
        avg[key] = statistics.mean(values) if values else None
    return avg
