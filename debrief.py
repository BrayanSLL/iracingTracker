"""Débrief automatique d'une session : points forts, points à améliorer et priorité.

Tout est calculé à partir des temps au tour, des secteurs et de la télémétrie des virages.
Les remarques « à améliorer » ont un poids (≈ temps gagnable par tour, en secondes) qui sert
à choisir la priorité pour la prochaine session.
"""
import statistics

import analysis
import db
import technique

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
    habits = []  # remarques de pilotage de chaque tour analysé
    for lap in others:
        trace = db.get_trace(lap["id"])
        if not trace:
            continue
        result = analysis.compare(trace, best_trace, points=DEBRIEF_POINTS)
        for corner in analysis.corner_analysis(result, length):
            per_corner.setdefault(corner["number"], []).append(corner)
        habits.append(technique.lap_report(result, session))
    if not per_corner:
        return
    _habits(habits, good, bad)

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


# --- comparaison de deux sessions --------------------------------------------------------------

def _date(iso):
    return f"{iso[8:10]}/{iso[5:7]}"


def compare_sessions(a_id, b_id):
    """Compare la session A (avant) et la session B (après) : temps, régularité, secteurs, virages."""
    sessions = {s["id"]: s for s in db.query("SELECT * FROM sessions WHERE id IN (?, ?)", (a_id, b_id))}
    if a_id not in sessions or b_id not in sessions:
        return None
    a, b = sessions[a_id], sessions[b_id]
    laps_a, laps_b = db.list_laps(a_id), db.list_laps(b_id)
    stats_a, stats_b = analysis.session_stats(laps_a), analysis.session_stats(laps_b)
    same_track = (a["car"], a["track"]) == (b["car"], b["track"])

    metrics = []
    for key, label, lower_is_better in (
        ("best_lap", "Meilleur tour", True), ("avg_lap", "Moyenne", True), ("stdev", "Régularité (écart-type)", True),
        ("ideal_lap", "Tour idéal", True), ("clean_laps", "Tours propres", False), ("avg_fuel", "Conso / tour", None),
    ):
        va, vb = stats_a[key], stats_b[key]
        diff = vb - va if va is not None and vb is not None else None
        better = None if diff is None or lower_is_better is None or abs(diff) < 1e-9 else (diff < 0) == lower_is_better
        metrics.append({"key": key, "label": label, "a": va, "b": vb, "diff": diff, "better": better})

    sectors = None
    if stats_a["best_sectors"] and stats_b["best_sectors"]:
        sectors = [sb - sa for sa, sb in zip(stats_a["best_sectors"], stats_b["best_sectors"])]

    corners = []
    trace_a = db.get_trace(stats_a["best_lap_id"]) if stats_a["best_lap_id"] else None
    trace_b = db.get_trace(stats_b["best_lap_id"]) if stats_b["best_lap_id"] else None
    if same_track and trace_a and trace_b:
        result = analysis.compare(trace_b, trace_a, points=3000)
        corners = [{"number": c["number"], "time_lost": c["time_lost"], "advice": c["advice"],
                    "a": c["ref"], "b": c["lap"]} for c in analysis.corner_analysis(result, a["track_length_m"])]

    return {"a": a, "b": b, "same_track": same_track, "metrics": metrics, "sectors": sectors,
            "corners": corners, "verdict": _verdict(a, b, metrics, corners, same_track)}


def _verdict(a, b, metrics, corners, same_track):
    lines = []
    if not same_track:
        lines.append({"tone": "neutral", "text": "Attention : les deux sessions n'ont pas la même voiture ou le même circuit, "
                                                  "la comparaison n'a pas vraiment de sens."})
    m = {x["key"]: x for x in metrics}
    best = m["best_lap"]
    if best["diff"] is not None:
        faster = best["diff"] < 0
        lines.append({"tone": "good" if faster else "bad",
                      "text": f"Meilleur tour {'plus rapide' if faster else 'plus lent'} de {_s(abs(best['diff']), 3)} s "
                              f"({_fmt_time(best['a'])} → {_fmt_time(best['b'])})."})
    avg = m["avg_lap"]
    if avg["diff"] is not None and abs(avg["diff"]) >= 0.05:
        lines.append({"tone": "good" if avg["diff"] < 0 else "bad",
                      "text": f"Rythme moyen {'meilleur' if avg['diff'] < 0 else 'moins bon'} de {_s(abs(avg['diff']))} s par tour."})
    stdev = m["stdev"]
    if stdev["diff"] is not None and abs(stdev["diff"]) >= 0.05:
        lines.append({"tone": "good" if stdev["diff"] < 0 else "bad",
                      "text": f"{'Plus' if stdev['diff'] < 0 else 'Moins'} régulier : ± {_s(stdev['a'], 3)} s → ± {_s(stdev['b'], 3)} s."})
    gains = sorted((c for c in corners if c["time_lost"] <= -0.05), key=lambda c: c["time_lost"])[:2]
    losses = sorted((c for c in corners if c["time_lost"] >= 0.05), key=lambda c: -c["time_lost"])[:2]
    if gains:
        lines.append({"tone": "good", "text": "Gains surtout au " + " et au ".join(
            f"virage {c['number']} (−{_s(-c['time_lost'])} s)" for c in gains) + "."})
    if losses:
        lines.append({"tone": "bad", "text": "Pertes surtout au " + " et au ".join(
            f"virage {c['number']} (+{_s(c['time_lost'])} s)" for c in losses) + "."})
    if b.get("note"):
        lines.append({"tone": "neutral", "text": f"Ta note sur la session B : « {b['note']} »."})
    return lines


# --- débrief de progression sur plusieurs sessions ---------------------------------------------

_progress_cache = {}


def progress_debrief(car, track):
    """Évolution sur toutes les sessions d'un couple voiture × circuit : ce qui progresse, ce qui stagne."""
    signature = db.query_one("""SELECT COUNT(l.id) AS n, MAX(l.id) AS last FROM laps l
                                JOIN sessions s ON s.id = l.session_id WHERE s.car IS ? AND s.track IS ?""",
                             (car, track))
    key = (car, track, signature["n"], signature["last"])
    if key not in _progress_cache:
        if len(_progress_cache) > 50:
            _progress_cache.clear()
        _progress_cache[key] = _build_progress(car, track)
    return _progress_cache[key]


def _build_progress(car, track):
    sessions = []
    for row in db.query("SELECT * FROM sessions WHERE car IS ? AND track IS ? ORDER BY started_at, id", (car, track)):
        laps = db.list_laps(row["id"])
        stats = analysis.session_stats(laps)
        if stats["clean_laps"] >= 3:
            sessions.append({"session": row, "laps": laps, "stats": stats})
    if len(sessions) < 2:
        return {"ready": False, "good": [], "bad": [], "priority": None,
                "message": f"Il faut au moins 2 sessions avec 3 tours propres sur ce circuit ({len(sessions)} pour l'instant)."}

    half = len(sessions) // 2
    early, recent = sessions[:half], sessions[half:]
    since = _date(sessions[0]["session"]["started_at"])
    good, bad = [], []
    mean = statistics.mean

    first_best = sessions[0]["stats"]["best_lap"]
    record = min(s["stats"]["best_lap"] for s in sessions)
    if first_best - record >= 0.05:
        good.append(_remark(f"Record amélioré de {_s(first_best - record)} s depuis le {since}",
                            f"{_fmt_time(first_best)} → {_fmt_time(record)} en {len(sessions)} sessions."))

    early_avg = [s["stats"]["avg_lap"] for s in early]
    recent_avg = [s["stats"]["avg_lap"] for s in recent]
    pace_gain = mean(early_avg) - mean(recent_avg)
    if pace_gain >= 0.2:
        good.append(_remark(f"Rythme moyen en progrès : −{_s(pace_gain)} s par tour",
                            "Entre tes premières et tes dernières sessions sur ce circuit."))
    elif pace_gain <= -0.2:
        bad.append(_remark(f"Rythme moyen en baisse : +{_s(-pace_gain)} s par tour",
                           "Tes dernières sessions sont plus lentes en moyenne que les premières.", weight=0.2))

    stdev_e = [s["stats"]["stdev"] for s in early if s["stats"]["stdev"] is not None and s["stats"]["clean_laps"] >= 5]
    stdev_r = [s["stats"]["stdev"] for s in recent if s["stats"]["stdev"] is not None and s["stats"]["clean_laps"] >= 5]
    if stdev_e and stdev_r:
        before, after = mean(stdev_e), mean(stdev_r)
        if before - after >= 0.1:
            good.append(_remark(f"Plus régulier qu'avant : ± {_s(before, 3)} s → ± {_s(after, 3)} s", ""))
        elif after >= 0.4 and before - after < 0.05:
            bad.append(_remark(f"Ta régularité stagne (± {_s(after, 3)} s)",
                               "Elle ne s'améliore pas d'une session à l'autre. Fais des relais de 10 tours "
                               "en visant le même temps à chaque tour.", weight=after * 0.4))

    _progress_sectors(early, recent, since, good, bad)
    _progress_corners(car, track, sessions, early, recent, since, good, bad)

    priority = max(bad, key=lambda r: r["weight"]) if bad else None
    return {"ready": True, "good": good, "bad": sorted(bad, key=lambda r: -r["weight"]),
            "priority": priority if priority and priority["weight"] > 0 else None, "message": None,
            "period": {"since": sessions[0]["session"]["started_at"], "until": sessions[-1]["session"]["started_at"],
                       "sessions": len(sessions)}}


def _sector_spreads(group):
    """Écart-type moyen de chaque secteur sur un groupe de sessions."""
    per_session = []
    for s in group:
        rows = [l["sectors"] for l in s["laps"]
                if l["flag"] is None and l["lap_time"] is not None and l["sectors"]
                and len(l["sectors"]) == analysis.SECTOR_COUNT]
        if len(rows) >= 5:
            per_session.append([statistics.stdev(col) for col in zip(*rows)])
    return [statistics.mean(col) for col in zip(*per_session)] if per_session else None


def _progress_sectors(early, recent, since, good, bad):
    before, after = _sector_spreads(early), _sector_spreads(recent)
    if not before or not after:
        return
    changes = [b - a for b, a in zip(before, after)]  # positif = plus régulier qu'avant
    improved = max(range(len(changes)), key=lambda i: changes[i])
    worst_now = max(range(len(after)), key=lambda i: after[i])
    if changes[improved] >= 0.05:
        good.append(_remark(f"S{improved + 1} beaucoup plus régulier depuis le {since}",
                            f"± {_s(before[improved], 3)} s → ± {_s(after[improved], 3)} s."))
    if after[worst_now] >= 0.15 and changes[worst_now] < 0.03:
        bad.append(_remark(f"Ta régularité en S{worst_now + 1} stagne (± {_s(after[worst_now], 3)} s)",
                           "C'est toujours le secteur le plus irrégulier, session après session.",
                           weight=after[worst_now]))


def _progress_corners(car, track, sessions, early, recent, since, good, bad):
    record_row = db.record_lap(car, track)
    record_trace = db.get_trace(record_row["id"]) if record_row else None
    if not record_trace:
        return
    length = sessions[0]["session"]["track_length_m"]
    corner_times = {}  # id de session -> {virage: temps perdu sur le record}
    for s in sessions:
        trace = db.get_trace(s["stats"]["best_lap_id"])
        if not trace:
            continue
        result = analysis.compare(trace, record_trace, points=DEBRIEF_POINTS)
        corner_times[s["session"]["id"]] = {c["number"]: c["time_lost"] for c in analysis.corner_analysis(result, length)}

    def average(group, number):
        values = [corner_times[s["session"]["id"]][number] for s in group
                  if s["session"]["id"] in corner_times and number in corner_times[s["session"]["id"]]]
        return statistics.mean(values) if values else None

    numbers = sorted({n for times in corner_times.values() for n in times})
    trends = []
    for n in numbers:
        before, after = average(early, n), average(recent, n)
        if before is not None and after is not None:
            trends.append({"number": n, "gain": before - after, "gap_now": after})
    for t in sorted((t for t in trends if t["gain"] >= 0.05), key=lambda t: -t["gain"])[:2]:
        good.append(_remark(f"Virage {t['number']} : −{_s(t['gain'])} s depuis le {since}",
                            "Tes meilleurs tours y sont nettement plus rapides qu'au début."))
    for t in sorted((t for t in trends if t["gain"] <= -0.05), key=lambda t: t["gain"])[:1]:
        bad.append(_remark(f"Virage {t['number']} : +{_s(-t['gain'])} s par rapport à tes débuts",
                           "Tu y étais plus rapide lors de tes premières sessions. Compare avec un ancien tour.",
                           weight=-t["gain"]))
    stuck = [t for t in trends if t["gap_now"] >= 0.1 and abs(t["gain"]) < 0.03]
    if stuck:
        t = max(stuck, key=lambda t: t["gap_now"])
        bad.append(_remark(f"Virage {t['number']} stagne",
                           f"Toujours environ {_s(t['gap_now'])} s de plus que sur ton record, sans amélioration. "
                           "Un bon candidat pour le mode entraînement.", weight=t["gap_now"]))


HABIT_SHARE = 0.4   # une erreur présente sur au moins 40 % des tours analysés devient une « habitude »


def _habits(reports, good, bad):
    """Erreurs de pilotage qui reviennent tour après tour (analyse de technique.py)."""
    laps = len(reports)
    if laps < 3:
        return
    counts, examples = {}, {}
    for report in reports:
        for r in {(r["corner"], r["code"]): r for r in report["remarks"]}.values():
            key = (r["corner"], r["code"])
            counts[key] = counts.get(key, 0) + 1
            examples.setdefault(key, r)
    habits = sorted(((n, key) for key, n in counts.items() if n >= max(2, HABIT_SHARE * laps)),
                    key=lambda item: -item[0])  # la clé peut contenir None (erreur « sur tout le tour »)
    for n, key in habits[:4]:
        r = examples[key]
        where = f"virage {r['corner']}" if r["corner"] else "sur tout le tour"
        bad.append(_remark(f"Habitude · {r['topic'].lower()} ({where}) : {n} tours sur {laps}",
                           f"{r['observation']} {r['action']}", weight=0.08 * r["severity"] * n / laps))
    with_issues = {examples[key]["topic"] for key in counts}
    missing = {m for report in reports for m in report["missing"]}
    measurable = [t for t in technique.TOPICS
                  if not (t == "Rapports" and "régime moteur" in missing) and not (t == "Volant" and "accélération latérale" in missing)]
    clean_topics = [t for t in measurable if t not in with_issues]
    if clean_topics:
        good.append(_remark("Technique propre : " + ", ".join(t.lower() for t in clean_topics),
                            "Aucune erreur de pilotage détectée dans ces domaines sur les tours analysés."))
