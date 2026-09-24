"""Analyse du pilotage façon ingénieur de course : freinage, accélération, rapports, volant.

Pour chaque virage d'un tour (comparé à une référence quand il y en a une), on mesure la forme
des courbes et on produit des remarques au format : observation → conséquence → action.

Mesures (sur la grille de distance commune de analysis.compare) :
- freinage : temps pour atteindre la pression max (attaque), pression max, ABS, relâchement pendant
  qu'on tourne (trail braking), chevauchement frein / gaz
- accélération : hésitations (on remet les gaz puis on relâche), temps pour passer à fond
- rapports : régime au point de corde, passages de rapport trop tôt, rupteur
- volant : corrections (coups de volant répétés), sous-virage (plus de volant pour moins de G latéral)

Tous les seuils sont regroupés ci-dessous : ils dépendent de la voiture et sont à ajuster.
"""
import analysis

# --- seuils ---------------------------------------------------------------------------------
BRAKE_ON = 0.10            # pédale de frein considérée enfoncée
ATTACK_SLOW = 0.25         # s pour atteindre 90 % de la pression max : au-delà, attaque « timide »
ATTACK_MARGIN = 0.10       # s de plus que la référence pour le signaler
PEAK_MARGIN = 0.10         # pression max inférieure de 10 points à la référence
ABS_HEAVY = 0.40           # ABS actif sur plus de 40 % du freinage
TRAIL_LAT_G = 0.30         # G latéral à partir duquel on considère qu'on tourne
TRAIL_SHORT = 0.15         # s de freinage en tournant : en dessous, relâché « d'un coup »
TRAIL_REF_MIN = 0.35       # … si la référence, elle, freine en tournant au moins 0,35 s
OVERLAP_MAX = 0.15         # s de frein et gaz en même temps
LIFT_DROP = 0.25           # un relâchement de 25 points de gaz = une hésitation
FULL_THROTTLE_MARGIN = 0.30  # s de plus que la référence pour passer à fond
LUGGING_RATIO = 0.50       # régime au point de corde < 50 % du régime de passage : rapport trop long
LONGER_THAN_REF_RATIO = 0.70  # … ou < 70 % avec un rapport de plus que la référence
EARLY_SHIFT_RATIO = 0.92   # passage de rapport sous 92 % du régime conseillé
LIMITER_RATIO = 0.99       # au-delà de 99 % de la zone rouge : rupteur
STEER_SWING = 3.0          # ° : amplitude minimale d'un coup de volant
EXTRA_CORRECTIONS = 2      # coups de volant de plus que la référence
UNDERSTEER_RATIO = 0.85    # G latéral par degré de volant < 85 % de la référence

TOPICS = ("Freinage", "Accélération", "Rapports", "Volant")


def _time_between(t, indices):
    return sum(t[j] - t[j - 1] for j in indices if j > 0)


def _brake(ch, start, apex, end):
    brake, t = ch["brake"], ch["t"]
    onset = next((j for j in range(start, apex + 1) if brake[j] >= BRAKE_ON), None)
    if onset is None:
        return None  # virage pris sans freiner (simple lever de pied)
    peak_j = max(range(onset, apex + 1), key=lambda j: brake[j])
    peak = brake[peak_j]
    reach = next(j for j in range(onset, peak_j + 1) if brake[j] >= 0.9 * peak)
    lat = ch.get("lat")
    braking = [j for j in range(onset, end + 1) if brake[j] >= BRAKE_ON]
    abs_channel = ch.get("abs")
    return {
        "attack": t[reach] - t[onset],
        "peak": peak,
        "trail": _time_between(t, [j for j in range(onset, end + 1) if brake[j] > 0.05 and abs(lat[j]) >= TRAIL_LAT_G])
        if lat else None,
        "abs": sum(abs_channel[j] for j in braking) / len(braking) if abs_channel and braking else None,
        "overlap": _time_between(t, [j for j in range(start, end + 1) if brake[j] > BRAKE_ON and ch["throttle"][j] > 0.1]),
    }


def _throttle(ch, apex, end):
    throttle, t = ch["throttle"], ch["t"]
    start = next((j for j in range(apex, end + 1) if throttle[j] >= 0.1), None)
    if start is None:
        return None
    full = next((j for j in range(start, end + 1) if throttle[j] >= 0.95), None)
    # on cherche les hésitations jusqu'à la fin de la sortie (avant le freinage suivant)
    stop = next((j for j in range(start, end + 1) if ch["brake"][j] >= BRAKE_ON), end)
    lifts, top, dipping = 0, 0.0, False
    for j in range(start, stop + 1):
        top = max(top, throttle[j])
        if not dipping and top - throttle[j] >= LIFT_DROP:
            dipping = True
        elif dipping and throttle[j] >= top - 0.05:
            # hésitation = on relâche PUIS on remet les gaz ; un lever de pied avant le freinage suivant ne compte pas
            lifts, dipping = lifts + 1, False
    return {"to_full": t[full] - t[start] if full is not None else None, "lifts": lifts,
            "abrupt": full is not None and t[full] - t[start] < 0.1}


def _steering(ch, start, apex, end):
    steer = ch["steer"]
    step = max(1, (end - start) // 60)  # on lisse un peu pour ne pas compter le bruit
    samples = steer[start:end + 1:step]
    reversals, direction, extreme = 0, 0, samples[0] if samples else 0
    for v in samples[1:]:
        move = v - extreme
        if direction >= 0 and move <= -STEER_SWING or direction <= 0 and move >= STEER_SWING:
            if direction != 0:
                reversals += 1
            direction = -1 if move < 0 else 1
            extreme = v
        elif direction > 0 and v > extreme or direction < 0 and v < extreme:
            extreme = v
    lat = ch.get("lat")
    window = range(max(start, apex - step * 5), min(end, apex + step * 5) + 1)
    mean_steer = sum(abs(steer[j]) for j in window) / len(window)
    grip = (sum(abs(lat[j]) for j in window) / len(window)) / mean_steer if lat and mean_steer > 1 else None
    return {"reversals": reversals, "grip": grip, "steer": mean_steer}


def _gear(ch, apex):
    rpm = ch.get("rpm")
    return {"gear": ch["gear"][apex], "rpm": rpm[apex] if rpm else None}


def measure(ch, start, apex, end):
    """Toutes les mesures d'un virage pour un tour (ch = canaux ré-échantillonnés)."""
    real_apex = min(range(start, end + 1), key=lambda j: ch["speed"][j])
    return {"brake": _brake(ch, start, real_apex, end), "throttle": _throttle(ch, real_apex, end),
            "steering": _steering(ch, start, real_apex, end), "gear": _gear(ch, real_apex)}


def _remark(code, number, topic, observation, consequence, action, severity=1):
    return {"code": code, "corner": number, "topic": topic, "observation": observation,
            "consequence": consequence, "action": action, "severity": severity}


def _num(value, digits=2):
    return f"{value:.{digits}f}".replace(".", ",")


def corner_remarks(number, lap, ref, shift_rpm=None):
    """Remarques d'ingénieur pour un virage (lap et ref = résultats de measure)."""
    out = []
    b, rb = lap["brake"], ref["brake"] if ref else None
    if b:
        if b["attack"] > ATTACK_SLOW and (not rb or b["attack"] > rb["attack"] + ATTACK_MARGIN):
            ref_txt = f" contre {_num(rb['attack'])} s sur ta référence" if rb else ""
            out.append(_remark("brake_attack", number, "Freinage",
                               f"Tu mets {_num(b['attack'])} s à atteindre ta pression de freinage maximale{ref_txt}.",
                               "Les premiers mètres de freinage, où la voiture a le plus d'appui, sont sous-exploités : "
                               "tu dois freiner plus tôt ou plus longtemps.",
                               "Attaque la pédale franchement d'un coup, puis dose en relâchant.", 2))
        if rb and b["peak"] < rb["peak"] - PEAK_MARGIN:
            out.append(_remark("brake_peak", number, "Freinage",
                               f"Pression maximale de {b['peak'] * 100:.0f} % contre {rb['peak'] * 100:.0f} % sur ta référence.",
                               "La voiture ralentit moins fort : tu freines sur une distance plus longue.",
                               "Appuie plus fort sur la pédale dans la première moitié du freinage.", 2))
        if b["abs"] is not None and b["abs"] > ABS_HEAVY:
            out.append(_remark("brake_abs", number, "Freinage",
                               f"L'ABS est actif pendant {b['abs'] * 100:.0f} % du freinage.",
                               "Tu freines au-delà du grip : l'ABS rallonge la distance de freinage et la voiture tourne moins bien.",
                               "Freine juste sous le seuil de l'ABS, surtout en fin de freinage quand tu tournes.", 1))
        if b["trail"] is not None and b["trail"] < TRAIL_SHORT and rb and rb["trail"] is not None and rb["trail"] >= TRAIL_REF_MIN:
            out.append(_remark("brake_release", number, "Freinage",
                               f"Tu lâches le frein avant de tourner ({_num(b['trail'])} s de frein en courbe, "
                               f"contre {_num(rb['trail'])} s sur ta référence).",
                               "Sans charge sur l'avant au moment de braquer, la voiture sous-vire à l'entrée.",
                               "Garde un peu de frein en tournant et relâche progressivement jusqu'au point de corde (trail braking).", 2))
        if b["overlap"] > OVERLAP_MAX:
            out.append(_remark("overlap", number, "Freinage",
                               f"Frein et accélérateur enfoncés en même temps pendant {_num(b['overlap'])} s.",
                               "Tu freines contre le moteur : perte de vitesse et usure inutile.",
                               "Relâche complètement une pédale avant d'appuyer sur l'autre.", 1))
    th, rth = lap["throttle"], ref["throttle"] if ref else None
    if th:
        if th["lifts"]:
            brutal = " après une remise des gaz brutale" if th["abrupt"] else ""
            out.append(_remark("throttle_lift", number, "Accélération",
                               f"Tu relâches l'accélérateur {th['lifts']} fois en sortie{brutal}.",
                               "C'est le signe que l'arrière décroche ou que tu es entré trop vite : "
                               "chaque relâchement coûte de la vitesse sur toute la ligne droite suivante.",
                               "Remets les gaz plus progressivement, et seulement quand le volant commence à se redresser.", 2))
        elif rth and th["to_full"] is not None and rth["to_full"] is not None and th["to_full"] > rth["to_full"] + FULL_THROTTLE_MARGIN:
            out.append(_remark("throttle_slow", number, "Accélération",
                               f"Tu mets {_num(th['to_full'])} s à passer à fond, contre {_num(rth['to_full'])} s sur ta référence.",
                               "La vitesse de sortie est plus faible, et c'est toute la ligne droite qui est plus lente.",
                               "Ouvre en grand plus tôt : dès que la voiture est stable après le point de corde.", 2))
    g, rg = lap["gear"], ref["gear"] if ref else None
    too_low = shift_rpm and g["rpm"] is not None and (
        g["rpm"] < LUGGING_RATIO * shift_rpm and (not rg or g["gear"] > rg["gear"])
        or rg and g["gear"] > rg["gear"] and g["rpm"] < LONGER_THAN_REF_RATIO * shift_rpm)
    if too_low:
        lower = f"la {rg['gear']}e comme sur ta référence" if rg and rg["gear"] < g["gear"] else f"la {g['gear'] - 1}e"
        out.append(_remark("long_gear", number, "Rapports",
                           f"Tu es en {g['gear']}e à {g['rpm']:.0f} tr/min au point de corde "
                           f"(régime conseillé pour passer : {shift_rpm:.0f} tr/min).",
                           "Le moteur est hors de sa plage de puissance : la voiture manque de reprise en sortie.",
                           f"Essaie {lower} : tu seras dans la bonne plage de régime pour la remise des gaz.", 2))
    st, rst = lap["steering"], ref["steering"] if ref else None
    if rst and st["reversals"] >= rst["reversals"] + EXTRA_CORRECTIONS:
        out.append(_remark("steer_corrections", number, "Volant",
                           f"{st['reversals']} changements de direction du volant dans ce virage, contre {rst['reversals']} sur ta référence.",
                           "Les corrections montrent une voiture instable : chaque correction fait perdre de l'adhérence.",
                           "Braque une fois, franchement, au bon moment ; si tu dois corriger, c'est souvent que l'entrée était trop rapide.", 1))
    if rst and st["grip"] is not None and rst["grip"] and st["grip"] < UNDERSTEER_RATIO * rst["grip"] and st["steer"] > rst["steer"] * 1.1:
        out.append(_remark("understeer", number, "Volant",
                           f"Au point de corde, tu tournes le volant de {st['steer']:.0f}° pour {st['grip'] * st['steer']:.2f} G latéral "
                           f"(référence : {rst['steer']:.0f}° pour {rst['grip'] * rst['steer']:.2f} G).".replace(".", ","),
                           "Plus de volant pour moins de virage : la voiture sous-vire, les pneus avant glissent.",
                           "Entre un peu moins vite et garde du frein en tournant pour charger l'avant.", 2))
    return out


def shift_remarks(ch, shift_rpm, redline):
    """Passages de rapport sur tout le tour : trop tôt, ou au rupteur."""
    rpm, gear, t = ch.get("rpm"), ch["gear"], ch["t"]
    if not rpm or not shift_rpm:
        return [], None
    throttle = ch["throttle"]
    # seuls les passages sous pleine charge comptent (on ne juge pas un passage en ligne droite à mi-gaz)
    upshifts = [rpm[j - 1] for j in range(1, len(gear)) if gear[j] > gear[j - 1] > 0 and throttle[j - 1] >= 0.9]
    out = []
    summary = {"count": len(upshifts), "avg_rpm": sum(upshifts) / len(upshifts) if upshifts else None}
    if upshifts and summary["avg_rpm"] < EARLY_SHIFT_RATIO * shift_rpm:
        out.append(_remark("early_shift", None, "Rapports",
                           f"Tu passes les rapports à {summary['avg_rpm']:.0f} tr/min en moyenne ; le régime conseillé est {shift_rpm:.0f} tr/min.",
                           "En passant trop tôt, tu retombes sous la plage de puissance du rapport suivant : accélération plus lente.",
                           "Passe au témoin de changement de rapport (quand les voyants s'allument tous).", 2))
    if redline:
        limiter = _time_between(t, [j for j in range(len(rpm)) if rpm[j] >= LIMITER_RATIO * redline])
        summary["limiter"] = limiter
        if limiter > 0.3:
            out.append(_remark("limiter", None, "Rapports",
                               f"{_num(limiter, 1)} s passées au rupteur sur le tour.",
                               "Au rupteur, le moteur coupe : la voiture n'accélère plus.",
                               "Passe le rapport supérieur un peu plus tôt, dès le témoin de changement de rapport.", 1))
    return out, summary


def lap_report(result, meta):
    """Rapport complet d'un tour : remarques par virage, passages de rapport, profil de pilotage."""
    lap = result["lap"]
    missing = [name for key, name in (("rpm", "régime moteur"), ("lat", "accélération latérale"), ("abs", "ABS"))
               if not lap.get(key)]
    base = result.get("ref") or lap
    remarks = []
    for number, (start, apex, end) in enumerate(analysis.find_corners(base["speed"]), start=1):
        mine = measure(lap, start, apex, end)
        theirs = measure(result["ref"], start, apex, end) if result.get("ref") else None
        remarks += corner_remarks(number, mine, theirs, meta.get("shift_rpm"))
    shifts, shift_summary = shift_remarks(lap, meta.get("shift_rpm"), meta.get("redline_rpm"))
    remarks += shifts
    remarks.sort(key=lambda r: (-r["severity"], r["corner"] or 0))
    profile = []
    for topic in TOPICS:
        count = sum(1 for r in remarks if r["topic"] == topic)
        status = "solide" if count == 0 else "à surveiller" if count <= 2 else "à travailler"
        profile.append({"topic": topic, "count": count, "status": status})
    return {"remarks": remarks, "profile": profile, "shifts": shift_summary, "missing": missing,
            "has_reference": bool(result.get("ref"))}
