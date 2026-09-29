"""Lecture de la télémétrie (iRacing ou Le Mans Ultimate, mémoire partagée) et enregistrement des tours."""
import collections
import re
import threading
import time

import analysis
import db
import debrief
import objectives
from voice import speaker, spoken_delta, spoken_time

MS_TO_KMH = 3.6
RAD_TO_DEG = 57.29578
G = 9.81
LAP_TIME_TIMEOUT = 3.0  # secondes d'attente max pour que LapLastLapTime se mette à jour
INPUT_HISTORY = 360  # échantillons de pédales gardés pour la fenêtre des pédales (6 s à 60 Hz)


class DataValidEvent:
    """Attend la prochaine mise à jour d'iRacing (événement Windows `IRSDKDataValidEvent`).

    Bien plus précis qu'un time.sleep(1/60), qui sur Windows peut dormir 15 ms ou plus
    et faire rater des échantillons. Hors Windows (tests, démo), on se rabat sur sleep.
    """

    def __init__(self):
        self.handle = None
        try:
            import ctypes
            from ctypes import wintypes
            self.kernel32 = ctypes.windll.kernel32
            self.kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
            self.kernel32.OpenEventW.restype = wintypes.HANDLE
            self.kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        except (ImportError, AttributeError):
            self.kernel32 = None

    def wait(self):
        if self.kernel32 and not self.handle:
            self.handle = self.kernel32.OpenEventW(0x00100000, False, "Local\\IRSDKDataValidEvent")
        if self.handle:
            self.kernel32.WaitForSingleObject(self.handle, 100)
        else:
            time.sleep(1 / 60)

    def reset(self):
        """À appeler quand iRacing se ferme : l'ancien événement ne sera plus jamais signalé."""
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
        self.handle = None


def parse_track_length(text):
    """'7.00 km' -> 7000.0 ; '4.32 mi' -> 6952.3"""
    match = re.match(r"\s*([\d.]+)\s*(km|mi)", text or "")
    if not match:
        return None
    value = float(match.group(1))
    return value * (1609.344 if match.group(2) == "mi" else 1000.0)


def default_sources():
    """Simulateurs pris en charge, essayés dans cet ordre : (lecteur façon pyirsdk, attente, nom)."""
    import lmu
    sources = []
    try:
        import irsdk  # pip install pyirsdk
        sources.append((irsdk.IRSDK(), DataValidEvent(), "iRacing"))
    except ImportError:
        print("[telemetry] pyirsdk absent : iRacing ne sera pas détecté (pip install pyirsdk)")
    sources.append((lmu.LMUSDK(), lmu.LMUWaiter(), lmu.LMUSDK.name))
    return sources


class TelemetryRecorder:
    """Lit le simulateur lancé, détecte les tours et enregistre chaque tour avec sa trace complète."""

    def __init__(self, ir=None, waiter=None, sources=None):
        if ir is not None:
            sources = [(ir, waiter or DataValidEvent(), getattr(ir, "name", "iRacing"))]
        self.sources = sources or default_sources()
        self.ir, self.waiter, self.sim = self.sources[0]
        self.lock = threading.Lock()
        self.connected = False
        self.session_id = None
        self.track = None
        self.car = None
        self.live = {}
        self.record = None  # {"lap_time", "d", "t"} : ton record sur ce couple voiture × circuit
        self.training = None  # virage choisi pour le mode entraînement
        self.inputs = collections.deque(maxlen=INPUT_HISTORY)  # (n°, gaz, frein) à 60 Hz
        self.input_seq = 0
        self._reset_lap_tracking()

    # --- état interne -------------------------------------------------------

    def _reset_lap_tracking(self):
        self.session_num = None
        self.was_on_track = False
        self.stint_clean_laps = 0  # tours propres depuis la dernière sortie des stands (pour le résumé radio)
        self.last_tick = None
        self.last_lap_completed = None
        self.prev_last_lap_time = None
        self.pending = None  # tour terminé qui attend son temps officiel
        self._reset_lap_stats()

    def _reset_lap_stats(self, flag=None, fuel_start=None, lap_start=None):
        self.samples = 0
        self.throttle_sum = 0.0
        self.brake_sum = 0.0
        self.max_speed = 0.0
        self.flag = flag
        self.fuel_start = fuel_start
        self.lap_start = lap_start  # SessionTime au début du tour
        self.trace = {"t": [], "d": [], "speed": [], "throttle": [], "brake": [], "gear": [], "steer": [], "yaw": [],
                      "rpm": [], "lat": [], "lon": [], "abs": [], "clutch": []}

    # --- connexion ----------------------------------------------------------

    def _check_connection(self):
        if self.connected and not (self.ir.is_initialized and self.ir.is_connected):
            self.ir.shutdown()
            self.waiter.reset()
            with self.lock:
                self.connected = False
                self.session_id = None
                self.live = {}
            print(f"[telemetry] {self.sim} déconnecté")
        elif not self.connected:
            for ir, waiter, sim in self.sources:  # le premier simulateur lancé l'emporte
                if ir.startup() and ir.is_initialized and ir.is_connected:
                    with self.lock:
                        self.ir, self.waiter, self.sim = ir, waiter, sim
                        self.connected = True
                    self._reset_lap_tracking()
                    print(f"[telemetry] {sim} connecté")
                    break
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
        track_length = parse_track_length(weekend.get("TrackLength"))

        # régime de passage conseillé (témoin de changement de rapport) et zone rouge de la voiture
        shift_rpm = driver_info.get("DriverCarSLShiftRPM") or None
        redline = driver_info.get("DriverCarRedLine") or None
        session_id = db.create_session(track, track_length, car, session_type, shift_rpm, redline)
        objectives.ensure_car(car, track)  # voiture / circuit inconnus : on réplique les objectifs
        with self.lock:
            self.session_id, self.track, self.car = session_id, track, car
        self._load_record()  # après avoir mis à jour la voiture et le circuit
        print(f"[telemetry] nouvelle session #{session_id} : {track} / {car} ({session_type})")

    def _load_record(self):
        """Charge la trace de ton meilleur tour ici (toutes sessions) pour le delta en direct."""
        row = db.record_lap(self.car, self.track)
        trace = db.get_trace(row["id"]) if row else None
        self.record = {"lap_time": row["lap_time"], "d": trace["d"], "t": trace["t"]} if trace else None
        self._update_training_ref()

    # --- mode entraînement --------------------------------------------------------

    def set_training(self, car, track, number, d0, d1):
        with self.lock:
            self.training = {"car": car, "track": track, "number": number, "d0": d0, "d1": d1,
                             "attempts": [], "in_zone": False, "entry": None, "live": None}
        self._update_training_ref()

    def clear_training(self):
        with self.lock:
            self.training = None

    def _update_training_ref(self):
        tr = self.training
        if not tr:
            return
        rec = self.record if (tr["car"], tr["track"]) == (self.car, self.track) else None
        if rec is None and (tr["car"], tr["track"]) != (self.car, self.track):
            row = db.record_lap(tr["car"], tr["track"])
            trace = db.get_trace(row["id"]) if row else None
            rec = {"d": trace["d"], "t": trace["t"]} if trace else None
        tr["ref"] = rec
        tr["ref_time"] = (analysis._interp(rec["d"], rec["t"], tr["d1"]) - analysis._interp(rec["d"], rec["t"], tr["d0"])
                          if rec else None)

    def _training_step(self, prev_d, prev_t, d, t, lap):
        """Chronomètre le virage choisi à chaque passage (entrée et sortie interpolées entre deux mesures)."""
        tr = self.training
        if not tr or (tr["car"], tr["track"]) != (self.car, self.track) or d <= prev_d:
            if tr and d < prev_d - 0.5:
                tr["in_zone"] = False  # passage de ligne
            return
        d0, d1 = tr["d0"], tr["d1"]
        if not tr["in_zone"] and prev_d < d0 <= d:
            tr["entry"] = prev_t + (d0 - prev_d) / (d - prev_d) * (t - prev_t)
            tr["in_zone"] = True
        if not tr["in_zone"]:
            return
        if prev_d < d1 <= d:
            exit_time = prev_t + (d1 - prev_d) / (d - prev_d) * (t - prev_t)
            corner_time = exit_time - tr["entry"]
            best = min((a["time"] for a in tr["attempts"]), default=None)
            reference = tr.get("ref_time") or best
            delta = corner_time - reference if reference else None
            with self.lock:
                tr["attempts"] = (tr["attempts"] + [{"lap": lap, "time": corner_time, "delta": delta,
                                                     "personal_best": best is None or corner_time < best}])[-30:]
                tr["in_zone"] = False
                tr["live"] = None
            if delta is not None and db.get_setting("voice") != "off":
                speaker.say(f"Virage {tr['number']}, {spoken_delta(delta)}")
        elif tr.get("ref"):
            ref = tr["ref"]
            ref_elapsed = analysis._interp(ref["d"], ref["t"], d) - analysis._interp(ref["d"], ref["t"], d0)
            tr["live"] = (t - tr["entry"]) - ref_elapsed

    def _training_status(self):
        tr = self.training
        if not tr:
            return None
        times = [a["time"] for a in tr["attempts"]]
        return {"car": tr["car"], "track": tr["track"], "number": tr["number"], "d0": tr["d0"], "d1": tr["d1"],
                "ref_time": tr.get("ref_time"), "attempts": tr["attempts"][-10:],
                "best": min(times) if times else None, "count": len(times),
                "active": (tr["car"], tr["track"]) == (self.car, self.track),
                "in_zone": tr["in_zone"], "live": tr["live"] if tr["in_zone"] else None}

    def _live_delta(self, lap_dist, lap_time):
        """Écart en direct avec le record, au même endroit du tour (positif = plus lent)."""
        if not self.record or lap_dist is None or lap_time is None or self.flag == "partial":
            return None
        if lap_time < 1.0 and lap_dist > 0.5:  # juste après la ligne, LapDistPct n'est pas encore revenu à 0
            return None
        ref_time = analysis._interp(self.record["d"], self.record["t"], lap_dist)
        if ref_time is None:
            return None
        delta = lap_time - ref_time
        return delta if abs(delta) < 30 else None  # valeur aberrante (instant du passage de ligne)

    # --- boucle -------------------------------------------------------------

    def _tick(self):
        ir = self.ir
        ir.freeze_var_buffer_latest()  # toutes les lectures viennent du même instant
        try:
            tick = ir["SessionTick"]
            if tick is not None and tick == self.last_tick:
                return  # pas de nouvelle donnée depuis la dernière lecture
            self.last_tick = tick

            session_num = ir["SessionNum"]
            if session_num is None:
                return
            if session_num != self.session_num:
                self._reset_lap_tracking()
                self.last_tick = tick
                self.session_num = session_num
                self._start_session(session_num)

            session_time = ir["SessionTime"] or 0.0
            last_lap_time = ir["LapLastLapTime"]
            fuel = ir["FuelLevel"]
            speed_kmh = (ir["Speed"] or 0.0) * MS_TO_KMH
            throttle = ir["Throttle"] or 0.0
            brake = ir["Brake"] or 0.0
            gear = ir["Gear"] or 0
            steer = (ir["SteeringWheelAngle"] or 0.0) * RAD_TO_DEG
            on_track = bool(ir["IsOnTrack"])
            on_pit_road = bool(ir["OnPitRoad"])

            current_lap_time = session_time - self.lap_start if self.lap_start is not None else None
            delta = self._live_delta(ir["LapDistPct"], current_lap_time) if on_track else None
            with self.lock:
                self.input_seq += 1
                self.inputs.append((self.input_seq, round(throttle, 3), round(brake, 3)))
                self.live = {"on_track": on_track, "speed_kmh": speed_kmh, "fuel_l": fuel,
                             "throttle": throttle, "brake": brake, "steer": steer, "gear": gear,
                             "lap": ir["Lap"], "lap_time": ir["LapCurrentLapTime"],
                             "delta": delta,
                             "record": self.record["lap_time"] if self.record else None,
                             "predicted": self.record["lap_time"] + delta if delta is not None else None}

            # 1. Un tour vient de finir : on attend que LapLastLapTime change
            #    (il est mis à jour avec un léger retard après la ligne).
            if self.pending:
                waited = time.monotonic() - self.pending["since"]
                if last_lap_time != self.pending["prev_time"] or waited > LAP_TIME_TIMEOUT:
                    self._save_lap(self.pending, last_lap_time)
                    self.pending = None

            # Retour au garage après un relais : l'ingénieur fait son résumé à la radio.
            if self.was_on_track and not on_track:
                self._radio_summary()
            self.was_on_track = on_track

            # 2. Au garage / dans les menus / en replay : rien à compter.
            if not on_track:
                self.last_lap_completed = None
                self.prev_last_lap_time = last_lap_time
                return

            lap_completed = ir["LapCompleted"]

            # 3. Première lecture en piste : on démarre en cours de tour.
            if self.last_lap_completed is None:
                self.last_lap_completed = lap_completed
                self._reset_lap_stats(flag="pit" if on_pit_road else "partial",
                                      fuel_start=fuel, lap_start=session_time)

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
                    "trace": self.trace,
                    "session_id": self.session_id,
                }
                self.last_lap_completed = lap_completed
                self._reset_lap_stats(fuel_start=fuel, lap_start=session_time)

            # Compteur qui recule (reset, nouvelle voiture…) : tour partiel.
            elif lap_completed < self.last_lap_completed:
                self.last_lap_completed = lap_completed
                self._reset_lap_stats(flag="partial", fuel_start=fuel, lap_start=session_time)

            # 5. Échantillon du tour en cours.
            self.samples += 1
            self.throttle_sum += throttle
            self.brake_sum += brake
            self.max_speed = max(self.max_speed, speed_kmh)
            if on_pit_road:
                self.flag = "pit"
            trace = self.trace
            if self.training and trace["d"]:
                self._training_step(trace["d"][-1], trace["t"][-1], ir["LapDistPct"] or 0.0,
                                    session_time - self.lap_start, ir["Lap"])
            trace["t"].append(round(session_time - self.lap_start, 4))
            trace["d"].append(round(ir["LapDistPct"] or 0.0, 6))
            trace["speed"].append(round(speed_kmh, 2))
            trace["throttle"].append(round(throttle, 4))
            trace["brake"].append(round(brake, 4))
            trace["gear"].append(gear)
            trace["steer"].append(round((ir["SteeringWheelAngle"] or 0.0) * RAD_TO_DEG, 1))
            trace["yaw"].append(round(ir["YawNorth"] or 0.0, 5))
            # analyse du pilotage : régime, accélérations (en G), ABS, embrayage
            trace["rpm"].append(round(ir["RPM"] or 0.0))
            trace["lat"].append(round((ir["LatAccel"] or 0.0) / G, 3))
            trace["lon"].append(round((ir["LongAccel"] or 0.0) / G, 3))
            trace["abs"].append(1 if ir["BrakeABSactive"] else 0)
            trace["clutch"].append(round(ir["Clutch"] if ir["Clutch"] is not None else 1.0, 3))

            self.prev_last_lap_time = last_lap_time
        finally:
            ir.unfreeze_var_buffer_latest()

    def _save_lap(self, lap, lap_time):
        if lap_time is not None and lap_time <= 0:
            lap_time = None  # iRacing renvoie -1 quand il n'y a pas de temps valide
        trace = analysis.clean_trace(lap["trace"]) if len(lap["trace"]["d"]) > 10 else None
        end_time = lap_time if lap_time is not None else (trace["t"][-1] if trace else None)
        record = dict(lap, lap_time=lap_time, sectors=analysis.sector_times(trace, end_time))
        try:
            db.insert_lap(lap["session_id"], record, trace)
        except Exception as exc:  # session supprimée entre-temps, disque plein…
            print(f"[telemetry] tour {lap['lap_number']} non enregistré : {exc!r}")
            return
        previous_record = self.record["lap_time"] if self.record else None
        if lap_time is not None and lap["flag"] is None and (previous_record is None or lap_time < previous_record):
            self._load_record()  # nouveau record : il devient la référence du delta en direct
        unlocked = []
        try:
            unlocked = objectives.evaluate(self.car, self.track)
        except Exception as exc:
            print(f"[objectifs] erreur : {exc!r}")
        self._announce_lap(lap_time, lap["flag"], previous_record, unlocked)

    def _radio_summary(self):
        if self.stint_clean_laps < 3 or db.get_setting("voice") == "off" or not self.session_id:
            return
        self.stint_clean_laps = 0
        session_id = self.session_id

        def speak():  # le débrief peut prendre une seconde : on ne bloque pas la capture
            try:
                text = debrief.radio_summary(session_id)
                if text:
                    speaker.say(text)
            except Exception as exc:
                print(f"[voix] résumé radio impossible : {exc!r}")
        threading.Thread(target=speak, daemon=True, name="radio").start()

    def _announce_lap(self, lap_time, flag, previous_record, unlocked):
        if lap_time is not None and flag is None:
            self.stint_clean_laps += 1
        mode = db.get_setting("voice")
        if mode == "off" or lap_time is None or flag is not None:
            return
        if previous_record is not None and lap_time < previous_record:
            speaker.say(f"Record battu ! {spoken_time(lap_time)}, {spoken_delta(lap_time - previous_record)}")
        elif mode == "laps":
            gap = f", {spoken_delta(lap_time - previous_record)}" if previous_record else ""
            speaker.say(f"{spoken_time(lap_time)}{gap}")
        if unlocked and mode == "laps":
            speaker.say("Objectif réussi" if len(unlocked) == 1 else f"{len(unlocked)} objectifs réussis")

    def run(self):
        while True:
            try:
                if self._check_connection():
                    self.waiter.wait()
                    self._tick()
                else:
                    time.sleep(1)  # aucun simulateur lancé : on réessaie chaque seconde
            except Exception as exc:  # on ne veut jamais tuer le thread
                print(f"[telemetry] erreur : {exc!r}")
                time.sleep(1)

    def start(self):
        threading.Thread(target=self.run, daemon=True, name="telemetry").start()

    def inputs_since(self, since):
        """Entrées pilote pour la fenêtre des pédales : valeurs en direct et échantillons après `since`."""
        with self.lock:
            live = self.live if self.connected else {}
            samples = [s[1:] for s in self.inputs if s[0] > since]
            return {"connected": self.connected, "on_track": bool(live.get("on_track")),
                    "speed_kmh": live.get("speed_kmh"), "gear": live.get("gear"), "steer": live.get("steer"),
                    "throttle": live.get("throttle"), "brake": live.get("brake"),
                    "seq": self.input_seq, "samples": samples}

    def status(self):
        with self.lock:
            return {"connected": self.connected, "sim": self.sim if self.connected else None,
                    "session_id": self.session_id,
                    "track": self.track, "car": self.car, **self.live,
                    "training": self._training_status()}
