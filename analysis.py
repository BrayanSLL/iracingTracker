"""Calculs sur les tours : nettoyage des traces, secteurs, comparaison, statistiques."""
import math
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


# --- carte du circuit ------------------------------------------------------------------------

def track_map(trace, grid):
    """Reconstitue le tracé à partir du cap (YawNorth) et de la vitesse, puis referme la boucle.

    Renvoie les coordonnées x/y (mètres) sur la grille de distance, ou None si la trace n'a pas de cap.
    """
    yaw = trace.get("yaw")
    if not yaw or len(yaw) < 10:
        return None
    t, speed = trace["t"], trace["speed"]
    xs, ys, dist = [0.0], [0.0], [0.0]
    for i in range(1, len(t)):
        step = max(0.0, t[i] - t[i - 1]) * speed[i] / 3.6
        xs.append(xs[-1] + step * math.sin(yaw[i]))
        ys.append(ys[-1] + step * math.cos(yaw[i]))
        dist.append(dist[-1] + step)
    total = dist[-1] or 1.0
    ex, ey = xs[-1] - xs[0], ys[-1] - ys[0]  # dérive accumulée : on la répartit sur le tour
    xs = [x - ex * d / total for x, d in zip(xs, dist)]
    ys = [y - ey * d / total for y, d in zip(ys, dist)]
    return {"x": [_interp(trace["d"], xs, g) for g in grid],
            "y": [_interp(trace["d"], ys, g) for g in grid]}


# --- analyse virage par virage ---------------------------------------------------------------

def _smooth(values, window):
    if window <= 1:
        return list(values)
    out, acc, n = [], 0.0, len(values)
    half = window // 2
    prefix = [0.0]
    for v in values:
        acc += v
        prefix.append(acc)
    for i in range(n):
        a, b = max(0, i - half), min(n, i + half + 1)
        out.append((prefix[b] - prefix[a]) / (b - a))
    return out


def find_corners(speed, min_drop=12.0):
    """Indices (début de freinage, point de corde, sortie) des virages, repérés par les minimums de vitesse."""
    n = len(speed)
    if n < 50:
        return []
    s = _smooth(speed, max(1, n // 300))
    span = max(1, n // 60)  # un minimum doit l'être sur ±1,7 % du tour
    apexes = []
    for i in range(1, n - 1):
        lo, hi = max(0, i - span), min(n, i + span + 1)
        if s[i] == min(s[lo:hi]) and (not apexes or i - apexes[-1] > span):
            apexes.append(i)
    corners = []
    for k, apex in enumerate(apexes):
        prev_apex = apexes[k - 1] if k else 0
        next_apex = apexes[k + 1] if k + 1 < len(apexes) else n - 1
        start = max(range(prev_apex, apex + 1), key=lambda j: s[j])
        end = max(range(apex, next_apex + 1), key=lambda j: s[j])
        if s[start] - s[apex] >= min_drop:
            corners.append((start, apex, end))
    return corners


def _corner_values(ch, d, start, end, to_pos):
    speed, brake, throttle, t = ch["speed"], ch["brake"], ch["throttle"], ch["t"]
    apex = min(range(start, end + 1), key=lambda j: speed[j])
    brake_i = next((j for j in range(start, apex + 1) if brake[j] >= 0.1), None)
    gas_i = next((j for j in range(apex, end + 1) if throttle[j] >= 0.5), None)
    coast = sum(t[j] - t[j - 1] for j in range(start + 1, end + 1) if throttle[j] < 0.05 and brake[j] < 0.05)
    return {
        "brake_point": to_pos(d[brake_i]) if brake_i is not None else None,
        "brake_max": max(brake[start:apex + 1]),
        "min_speed": speed[apex],
        "apex": to_pos(d[apex]),
        "throttle_point": to_pos(d[gas_i]) if gas_i is not None else None,
        "coast": coast,
        "time": t[end] - t[start],
    }


def corner_analysis(result, length_m=None):
    """Pour chaque virage : freinage, vitesse mini, remise des gaz, roue libre, temps perdu, conseils."""
    d = result["d"]
    base = result.get("ref") or result["lap"]
    to_pos = (lambda x: x * length_m) if length_m else (lambda x: x * 100)
    unit = "m" if length_m else "%"
    corners = []
    for number, (start, _, end) in enumerate(find_corners(base["speed"]), start=1):
        lap = _corner_values(result["lap"], d, start, end, to_pos)
        ref = _corner_values(result["ref"], d, start, end, to_pos) if result.get("ref") else None
        corner = {"number": number, "start": to_pos(d[start]), "end": to_pos(d[end]),
                  "lap": lap, "ref": ref, "time_lost": None, "advice": []}
        if ref:
            corner["time_lost"] = lap["time"] - ref["time"]
            corner["advice"] = _advice(lap, ref, unit)
        corners.append(corner)
    return corners


def _fmt_gap(value, unit):
    return f"{abs(value):.0f} m" if unit == "m" else f"{abs(value):.1f} %"


def _advice(lap, ref, unit):
    tips, tol = [], 5 if unit == "m" else 0.15
    if lap["brake_point"] is not None and ref["brake_point"] is not None:
        gap = lap["brake_point"] - ref["brake_point"]
        if gap < -tol:
            tips.append(f"Tu freines {_fmt_gap(gap, unit)} plus tôt")
        elif gap > tol:
            tips.append(f"Tu freines {_fmt_gap(gap, unit)} plus tard")
    if ref["brake_max"] - lap["brake_max"] >= 0.1:
        tips.append(f"Freinage moins appuyé ({lap['brake_max'] * 100:.0f} % contre {ref['brake_max'] * 100:.0f} %)")
    speed_gap = lap["min_speed"] - ref["min_speed"]
    if abs(speed_gap) >= 2:
        tips.append(f"Vitesse mini {'+' if speed_gap > 0 else '−'}{abs(speed_gap):.0f} km/h au point de corde")
    if lap["throttle_point"] is not None and ref["throttle_point"] is not None:
        gap = lap["throttle_point"] - ref["throttle_point"]
        if gap > tol:
            tips.append(f"Remise des gaz {_fmt_gap(gap, unit)} plus tard")
        elif gap < -tol:
            tips.append(f"Remise des gaz {_fmt_gap(gap, unit)} plus tôt")
    coast_gap = lap["coast"] - ref["coast"]
    if coast_gap >= 0.1:
        tips.append(f"+{coast_gap:.1f} s en roue libre (ni gaz ni frein)".replace(".", ","))
    return tips
