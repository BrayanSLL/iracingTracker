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
import random

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

# nom court de chaque erreur, pour les phrases d'historique (« plus de freinage timide au virage 2 »)
LABELS = {
    "brake_attack": "freinage timide", "brake_peak": "freinage peu appuyé", "brake_abs": "excès d'ABS",
    "brake_release": "frein relâché trop tôt", "overlap": "pédales qui se chevauchent",
    "throttle_lift": "hésitations à l'accélération", "throttle_slow": "remise des gaz lente",
    "long_gear": "rapport trop long", "steer_corrections": "corrections au volant", "understeer": "sous-virage",
    "early_shift": "passages de rapport trop tôt", "limiter": "rupteur",
}


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


# --- formulations ------------------------------------------------------------------------------
# Plusieurs versions de chaque remarque (observation, conséquence, action) pour que le rapport ne se
# répète pas d'un tour à l'autre. Le choix est stable pour un même tour (tiré au hasard à partir de
# l'identifiant du tour), donc le rapport ne change pas quand on le rouvre.

PHRASES = {
    "brake_attack": [
        ("Tu mets {attack} s à atteindre ta pression de freinage maximale{ref_txt}.",
         "Les premiers mètres de freinage, où la voiture a le plus d'appui, sont sous-exploités.",
         "Attaque la pédale franchement d'un coup, puis dose en relâchant."),
        ("Ton freinage démarre trop doucement : {attack} s avant d'être au maximum{ref_txt}.",
         "Au début du freinage, la voiture est à pleine vitesse et a le plus d'appui : c'est là qu'il faut freiner fort.",
         "Pense « coup de pied » sur la pédale, puis relâche progressivement."),
        ("Montée en pression lente sur ce freinage ({attack} s{ref_txt}).",
         "Tu perds des mètres de freinage efficace, donc tu dois commencer à freiner plus tôt.",
         "Pic de pression immédiat, puis relâchement progressif jusqu'au point de corde."),
    ],
    "brake_peak": [
        ("Pression maximale de {peak} % contre {ref_peak} % sur ta référence.",
         "La voiture ralentit moins fort : tu freines sur une distance plus longue.",
         "Appuie plus fort sur la pédale dans la première moitié du freinage."),
        ("Tu n'appuies qu'à {peak} % sur le frein, alors que ta référence monte à {ref_peak} %.",
         "Moins de pression, c'est un freinage plus long et un point de freinage plus tôt.",
         "Ose appuyer plus fort : tu as de la marge avant le blocage."),
        ("Freinage peu appuyé : {peak} % au maximum (référence : {ref_peak} %).",
         "Tu laisses du freinage sur la table.",
         "Travaille la force sur la pédale : un freinage court et fort vaut mieux qu'un long et doux."),
    ],
    "brake_abs": [
        ("L'ABS est actif pendant {abs} % du freinage.",
         "Tu freines au-delà du grip : l'ABS rallonge la distance de freinage et la voiture tourne moins bien.",
         "Freine juste sous le seuil de l'ABS, surtout en fin de freinage quand tu tournes."),
        ("Tu vis sur l'ABS : {abs} % du freinage avec l'ABS actif.",
         "L'ABS te sauve du blocage mais te fait perdre du freinage et de la précision en entrée.",
         "Relâche légèrement la pédale dès que tu sens l'ABS travailler."),
        ("Beaucoup d'ABS sur ce freinage ({abs} %).",
         "Pneus à la limite en permanence : moins de freinage et plus d'usure.",
         "Cherche la pression juste en dessous du déclenchement de l'ABS."),
    ],
    "brake_release": [
        ("Tu lâches le frein avant de tourner ({trail} s de frein en courbe, contre {ref_trail} s sur ta référence).",
         "Sans charge sur l'avant au moment de braquer, la voiture sous-vire à l'entrée.",
         "Garde un peu de frein en tournant et relâche progressivement jusqu'au point de corde (trail braking)."),
        ("Frein relâché d'un coup à l'entrée ({trail} s en courbe, référence : {ref_trail} s).",
         "L'avant se déleste au pire moment, juste quand tu as besoin qu'il morde.",
         "Relâche la pédale en douceur pendant que tu braques, comme un variateur."),
        ("Peu de trail braking ici : {trail} s de frein en tournant contre {ref_trail} s sur ta référence.",
         "Tu perds la rotation de la voiture en entrée de virage.",
         "Entre en freinant encore un peu, puis libère la pédale progressivement jusqu'à la corde."),
    ],
    "overlap": [
        ("Frein et accélérateur enfoncés en même temps pendant {overlap} s.",
         "Tu freines contre le moteur : perte de vitesse et usure inutile.",
         "Relâche complètement une pédale avant d'appuyer sur l'autre."),
        ("Les deux pédales se chevauchent ({overlap} s).",
         "Le moteur pousse pendant que les freins retiennent : tu gaspilles de la vitesse.",
         "Sépare bien les phases : frein, puis gaz."),
    ],
    "throttle_lift": [
        ("Tu relâches l'accélérateur {lifts} fois en sortie{brutal}.",
         "C'est le signe que l'arrière décroche ou que tu es entré trop vite : chaque relâchement coûte de la vitesse "
         "sur toute la ligne droite suivante.",
         "Remets les gaz plus progressivement, et seulement quand le volant commence à se redresser."),
        ("Hésitation à l'accélération : {lifts} relâchement{plural} en sortie{brutal}.",
         "La voiture n'est pas stable à la remise des gaz, et tu perds de la vitesse de sortie.",
         "Une seule remise des gaz, progressive, dès que tu commences à ouvrir le volant."),
        ("Sortie hachée : tu remets les gaz, tu relâches, puis tu remets ({lifts} fois){brutal}.",
         "Chaque hésitation se paie sur toute la ligne droite qui suit.",
         "Sois patient au point de corde, puis accélère d'un seul mouvement progressif."),
    ],
    "throttle_slow": [
        ("Tu mets {to_full} s à passer à fond, contre {ref_to_full} s sur ta référence.",
         "La vitesse de sortie est plus faible, et c'est toute la ligne droite qui est plus lente.",
         "Ouvre en grand plus tôt : dès que la voiture est stable après le point de corde."),
        ("Remise des gaz trop timide : {to_full} s avant d'être à fond (référence : {ref_to_full} s).",
         "Une sortie lente coûte du temps jusqu'au freinage suivant.",
         "Dès que tu redresses le volant, passe à fond franchement."),
        ("Passage à fond tardif en sortie ({to_full} s, contre {ref_to_full} s).",
         "Tu sors du virage moins vite que tu pourrais.",
         "Vise la pleine charge plus tôt : la voiture le supporte sur ta référence."),
    ],
    "long_gear": [
        ("Tu es en {gear}e à {rpm} tr/min au point de corde (régime conseillé pour passer : {shift} tr/min).",
         "Le moteur est hors de sa plage de puissance : la voiture manque de reprise en sortie.",
         "Essaie {lower} : tu seras dans la bonne plage de régime pour la remise des gaz."),
        ("Rapport trop long au point de corde : {gear}e à seulement {rpm} tr/min.",
         "Le moteur tourne trop bas pour relancer la voiture en sortie.",
         "Rétrograde d'un rapport de plus ({lower}) avant la corde."),
        ("{rpm} tr/min en {gear}e au point de corde, c'est trop bas pour ce moteur (passage conseillé à {shift}).",
         "Tu sors du virage sans reprise, et ça se paie dans la ligne droite.",
         "Prends {lower} pour ce virage."),
    ],
    "steer_corrections": [
        ("{n} changements de direction du volant dans ce virage, contre {ref_n} sur ta référence.",
         "Les corrections montrent une voiture instable : chaque correction fait perdre de l'adhérence.",
         "Braque une fois, franchement, au bon moment ; si tu dois corriger, c'est souvent que l'entrée était trop rapide."),
        ("Beaucoup de corrections au volant ici ({n}, contre {ref_n} sur ta référence).",
         "Tu te bats avec la voiture au lieu de la guider.",
         "Un seul mouvement de volant fluide ; si la voiture bouge, calme l'entrée."),
        ("Volant agité : {n} corrections dans le virage (référence : {ref_n}).",
         "Chaque coup de volant brusque déstabilise les pneus.",
         "Cherche des mouvements plus doux et plus anticipés."),
    ],
    "understeer": [
        ("Au point de corde, tu tournes le volant de {steer}° pour {g} G latéral (référence : {ref_steer}° pour {ref_g} G).",
         "Plus de volant pour moins de virage : la voiture sous-vire, les pneus avant glissent.",
         "Entre un peu moins vite et garde du frein en tournant pour charger l'avant."),
        ("Sous-virage : {steer}° de volant pour {g} G, alors que ta référence tourne mieux avec {ref_steer}°.",
         "Les pneus avant saturent : braquer plus ne sert à rien, ça les fait glisser davantage.",
         "Entre plus doucement et laisse la voiture tourner avant de rebraquer."),
        ("Tu braques plus pour tourner moins ({steer}° pour {g} G, contre {ref_steer}° pour {ref_g} G).",
         "C'est du sous-virage : l'avant ne suit plus.",
         "Réduis ta vitesse d'entrée et garde de la charge sur l'avant avec un peu de frein."),
    ],
    "early_shift": [
        ("Tu passes les rapports à {avg} tr/min en moyenne ; le régime conseillé est {shift} tr/min.",
         "En passant trop tôt, tu retombes sous la plage de puissance du rapport suivant : accélération plus lente.",
         "Passe au témoin de changement de rapport (quand les voyants s'allument tous)."),
        ("Passages de rapport trop tôt : {avg} tr/min en moyenne, pour un régime conseillé de {shift}.",
         "Tu n'exploites pas toute la puissance du moteur.",
         "Attends le témoin de changement de rapport avant de passer."),
        ("Tu changes de rapport trop tôt ({avg} tr/min au lieu de {shift}).",
         "Chaque rapport commence trop bas dans les tours : la voiture accélère moins.",
         "Garde le rapport jusqu'au témoin."),
    ],
    "limiter": [
        ("{limiter} s passées au rupteur sur le tour.",
         "Au rupteur, le moteur coupe : la voiture n'accélère plus.",
         "Passe le rapport supérieur un peu plus tôt, dès le témoin de changement de rapport."),
        ("Tu tapes le rupteur ({limiter} s sur le tour).",
         "Le temps passé au rupteur est du temps sans accélération.",
         "Anticipe un peu le passage de rapport."),
    ],
}


def _phrase(code, rng, **fields):
    observation, consequence, action = rng.choice(PHRASES[code])
    return observation.format(**fields), consequence.format(**fields), action.format(**fields)


def _remark(code, number, topic, rng, severity=1, **fields):
    observation, consequence, action = _phrase(code, rng, **fields)
    return {"code": code, "corner": number, "topic": topic, "observation": observation,
            "consequence": consequence, "action": action, "severity": severity}


def _num(value, digits=2):
    return f"{value:.{digits}f}".replace(".", ",")


def corner_remarks(number, lap, ref, shift_rpm=None, rng=None):
    """Remarques d'ingénieur pour un virage (lap et ref = résultats de measure)."""
    rng = rng or random.Random(number)
    out = []
    b, rb = lap["brake"], ref["brake"] if ref else None
    if b:
        if b["attack"] > ATTACK_SLOW and (not rb or b["attack"] > rb["attack"] + ATTACK_MARGIN):
            out.append(_remark("brake_attack", number, "Freinage", rng, 2, attack=_num(b["attack"]),
                               ref_txt=f" contre {_num(rb['attack'])} s sur ta référence" if rb else ""))
        if rb and b["peak"] < rb["peak"] - PEAK_MARGIN:
            out.append(_remark("brake_peak", number, "Freinage", rng, 2,
                               peak=f"{b['peak'] * 100:.0f}", ref_peak=f"{rb['peak'] * 100:.0f}"))
        if b["abs"] is not None and b["abs"] > ABS_HEAVY:
            out.append(_remark("brake_abs", number, "Freinage", rng, 1, abs=f"{b['abs'] * 100:.0f}"))
        if b["trail"] is not None and b["trail"] < TRAIL_SHORT and rb and rb["trail"] is not None and rb["trail"] >= TRAIL_REF_MIN:
            out.append(_remark("brake_release", number, "Freinage", rng, 2,
                               trail=_num(b["trail"]), ref_trail=_num(rb["trail"])))
        if b["overlap"] > OVERLAP_MAX:
            out.append(_remark("overlap", number, "Freinage", rng, 1, overlap=_num(b["overlap"])))
    th, rth = lap["throttle"], ref["throttle"] if ref else None
    if th:
        if th["lifts"]:
            out.append(_remark("throttle_lift", number, "Accélération", rng, 2, lifts=th["lifts"],
                               plural="s" if th["lifts"] > 1 else "",
                               brutal=" après une remise des gaz brutale" if th["abrupt"] else ""))
        elif rth and th["to_full"] is not None and rth["to_full"] is not None and th["to_full"] > rth["to_full"] + FULL_THROTTLE_MARGIN:
            out.append(_remark("throttle_slow", number, "Accélération", rng, 2,
                               to_full=_num(th["to_full"]), ref_to_full=_num(rth["to_full"])))
    g, rg = lap["gear"], ref["gear"] if ref else None
    too_low = shift_rpm and g["rpm"] is not None and (
        g["rpm"] < LUGGING_RATIO * shift_rpm and (not rg or g["gear"] > rg["gear"])
        or rg and g["gear"] > rg["gear"] and g["rpm"] < LONGER_THAN_REF_RATIO * shift_rpm)
    if too_low:
        lower = f"la {rg['gear']}e comme sur ta référence" if rg and rg["gear"] < g["gear"] else f"la {g['gear'] - 1}e"
        out.append(_remark("long_gear", number, "Rapports", rng, 2, gear=g["gear"], rpm=f"{g['rpm']:.0f}",
                           shift=f"{shift_rpm:.0f}", lower=lower))
    st, rst = lap["steering"], ref["steering"] if ref else None
    if rst and st["reversals"] >= rst["reversals"] + EXTRA_CORRECTIONS:
        out.append(_remark("steer_corrections", number, "Volant", rng, 1, n=st["reversals"], ref_n=rst["reversals"]))
    if rst and st["grip"] is not None and rst["grip"] and st["grip"] < UNDERSTEER_RATIO * rst["grip"] and st["steer"] > rst["steer"] * 1.1:
        out.append(_remark("understeer", number, "Volant", rng, 2, steer=f"{st['steer']:.0f}",
                           g=_num(st["grip"] * st["steer"]), ref_steer=f"{rst['steer']:.0f}",
                           ref_g=_num(rst["grip"] * rst["steer"])))
    return out


def shift_remarks(ch, shift_rpm, redline, rng=None):
    """Passages de rapport sur tout le tour : trop tôt, ou au rupteur."""
    rng = rng or random.Random(0)
    rpm, gear, t = ch.get("rpm"), ch["gear"], ch["t"]
    if not rpm or not shift_rpm:
        return [], None
    throttle = ch["throttle"]
    # seuls les passages sous pleine charge comptent (on ne juge pas un passage en ligne droite à mi-gaz)
    upshifts = [rpm[j - 1] for j in range(1, len(gear)) if gear[j] > gear[j - 1] > 0 and throttle[j - 1] >= 0.9]
    out = []
    summary = {"count": len(upshifts), "avg_rpm": sum(upshifts) / len(upshifts) if upshifts else None}
    if upshifts and summary["avg_rpm"] < EARLY_SHIFT_RATIO * shift_rpm:
        out.append(_remark("early_shift", None, "Rapports", rng, 2,
                           avg=f"{summary['avg_rpm']:.0f}", shift=f"{shift_rpm:.0f}"))
    if redline:
        limiter = _time_between(t, [j for j in range(len(rpm)) if rpm[j] >= LIMITER_RATIO * redline])
        summary["limiter"] = limiter
        if limiter > 0.3:
            out.append(_remark("limiter", None, "Rapports", rng, 1, limiter=_num(limiter, 1)))
    return out, summary


def lap_report(result, meta):
    """Rapport complet d'un tour : remarques par virage, passages de rapport, profil de pilotage."""
    lap = result["lap"]
    missing = [name for key, name in (("rpm", "régime moteur"), ("lat", "accélération latérale"), ("abs", "ABS"))
               if not lap.get(key)]
    base = result.get("ref") or lap
    remarks = []
    # formulations tirées au hasard mais toujours les mêmes pour un tour donné
    rng = random.Random(f"tour-{meta.get('id')}")
    for number, (start, apex, end) in enumerate(analysis.find_corners(base["speed"]), start=1):
        mine = measure(lap, start, apex, end)
        theirs = measure(result["ref"], start, apex, end) if result.get("ref") else None
        remarks += corner_remarks(number, mine, theirs, meta.get("shift_rpm"), rng)
    shifts, shift_summary = shift_remarks(lap, meta.get("shift_rpm"), meta.get("redline_rpm"), rng)
    remarks += shifts
    remarks.sort(key=lambda r: (-r["severity"], r["corner"] or 0))
    profile = []
    for topic in TOPICS:
        count = sum(1 for r in remarks if r["topic"] == topic)
        status = "solide" if count == 0 else "à surveiller" if count <= 2 else "à travailler"
        profile.append({"topic": topic, "count": count, "status": status})
    return {"remarks": remarks, "profile": profile, "shifts": shift_summary, "missing": missing,
            "has_reference": bool(result.get("ref"))}
