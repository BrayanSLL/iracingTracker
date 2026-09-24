"""Calculs sur les tours : nettoyage des traces, secteurs, comparaison, statistiques."""
import statistics
from bisect import bisect_left

SECTOR_COUNT = 10
CHANNELS = ("speed", "throttle", "brake", "gear", "steer")
STEP_CHANNELS = {"gear"}  # pas d'interpolation linéaire pour le rapport engagé
MAX_POINTS = 6000


def clean_trace(trace):
    """Enlève les échantillons « de l'autre côté de la ligne » et rend la distance croissante.

    Juste après la ligne, LapDistPct vaut parfois encore 0.99 ; juste avant, parfois déjà 0.01.
    """
    n = len(trace["d"])
    keep = [i for i, d in enumerate(trace["d"])
            if not (i < n * 0.1 and d > 0.9) and not (i > n * 0.9 and d < 0.1)]
    cleaned = {key: [values[i] for i in keep] for key, values in trace.items()}
    running = 0.0
    for i, d in enumerate(cleaned["d"]):  # un léger recul (tête-à-queue…) ne doit pas casser l'interpolation
        running = max(running, d)
        cleaned["d"][i] = running
    return cleaned


def _interp(xs, ys, x):
    """Interpolation linéaire de y(x) sur xs croissant."""
    if not xs:
        return None
    i = bisect_left(xs, x)
    if i <= 0:
        return ys[0]
    if i >= len(xs):
        return ys[-1]
    x0, x1 = xs[i - 1], xs[i]
    if x1 == x0:
        return ys[i]
    return ys[i - 1] + (ys[i] - ys[i - 1]) * (x - x0) / (x1 - x0)


def _time_at(trace, lap_time, d):
    if d <= 0:
        return 0.0
    if d >= 1:
        return lap_time
    return _interp(trace["d"], trace["t"], d)


def sector_times(trace, lap_time, count=SECTOR_COUNT):
    """Découpe le tour en `count` portions de même longueur et renvoie le temps de chacune."""
    if not trace or not trace["d"] or lap_time is None:
        return None
    bounds = [_time_at(trace, lap_time, k / count) for k in range(count + 1)]
    return [round(b - a, 3) for a, b in zip(bounds, bounds[1:])]


def resample(trace, grid):
    """Ré-échantillonne une trace sur une grille de distances commune (0..1)."""
    xs, out = trace["d"], {}
    for channel in ("t",) + CHANNELS:
        ys = trace.get(channel)
        if ys is None:
            continue
        if channel in STEP_CHANNELS:
            out[channel] = [ys[max(0, min(len(ys) - 1, bisect_left(xs, x) - 1))] for x in grid]
        else:
            out[channel] = [_interp(xs, ys, x) for x in grid]
    return out


def compare(lap_trace, ref_trace=None):
    """Aligne un tour (et éventuellement une référence) sur la distance, et calcule le delta."""
    points = min(MAX_POINTS, max(len(lap_trace["d"]), len(ref_trace["d"]) if ref_trace else 0))
    grid = [i / (points - 1) for i in range(points)]
    result = {"d": grid, "lap": resample(lap_trace, grid)}
    if ref_trace:
        result["ref"] = resample(ref_trace, grid)
        result["delta"] = [a - b for a, b in zip(result["lap"]["t"], result["ref"]["t"])]
    return result


def session_stats(laps):
    """Stats sur les tours propres (ni stand, ni partiel, avec un temps officiel)."""
    clean = [l for l in laps if l["flag"] is None and l["lap_time"] is not None]
    times = [l["lap_time"] for l in clean]
    stats = {
        "laps": len(laps),
        "clean_laps": len(clean),
        "best_lap": min(times) if times else None,
        "best_lap_id": min(clean, key=lambda l: l["lap_time"])["id"] if clean else None,
        "avg_lap": statistics.mean(times) if times else None,
        "stdev": statistics.stdev(times) if len(times) >= 2 else None,
        "avg_fuel": None,
        "ideal_lap": None,
        "best_sectors": None,
    }
    fuel = [l["fuel_used"] for l in clean if l["fuel_used"] is not None]
    if fuel:
        stats["avg_fuel"] = statistics.mean(fuel)
    sectors = [l["sectors"] for l in clean if l["sectors"] and len(l["sectors"]) == SECTOR_COUNT]
    if sectors:
        best = [min(column) for column in zip(*sectors)]
        stats["best_sectors"] = best
        stats["ideal_lap"] = sum(best)
    return stats
