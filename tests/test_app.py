"""Tests sans iRacing : la voiture simulée de demo.py tourne quelques tours.

    python -m unittest discover tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analysis  # noqa: E402
import db  # noqa: E402
import debrief  # noqa: E402
import main  # noqa: E402
import objectives  # noqa: E402
import technique  # noqa: E402
import telemetry  # noqa: E402
from demo import DemoWaiter, FakeIRSDK  # noqa: E402


def drive(recorder, fake, seconds):
    waiter = DemoWaiter(fake, speedup=0)  # pas de pause : simulation instantanée
    for _ in range(int(seconds * 60)):
        waiter.wait()
        if recorder._check_connection():
            recorder._tick()


class RecorderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(cls.tmp.name) / "sessions.db"
        db.init_db()
        cls.fake = FakeIRSDK(seed=42)
        cls.recorder = telemetry.TelemetryRecorder(ir=cls.fake, waiter=DemoWaiter(cls.fake, 0))
        drive(cls.recorder, cls.fake, 4 * 85)  # ~4 tours
        main.recorder = cls.recorder
        cls.client = main.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_laps_are_recorded_with_official_times(self):
        laps = db.list_laps(self.recorder.session_id)
        self.assertGreaterEqual(len(laps), 3)
        self.assertEqual(laps[0]["flag"], "pit")  # out-lap
        for lap in laps[1:]:
            self.assertIsNone(lap["flag"])
            self.assertTrue(75 < lap["lap_time"] < 95, lap["lap_time"])
            self.assertTrue(lap["has_trace"])
            self.assertAlmostEqual(sum(lap["sectors"]), lap["lap_time"], places=2)

    def test_trace_is_complete_and_precise(self):
        lap = db.list_laps(self.recorder.session_id)[1]
        trace = db.get_trace(lap["id"])
        self.assertGreater(len(trace["d"]), 60 * 75)  # ~60 échantillons par seconde
        self.assertEqual(trace["d"], sorted(trace["d"]))
        self.assertLess(trace["d"][0], 0.01)
        self.assertGreater(trace["d"][-1], 0.99)
        self.assertEqual(max(trace["throttle"]), 1.0)
        self.assertGreater(max(trace["brake"]), 0.5)

    def test_compare_aligns_laps(self):
        laps = db.list_laps(self.recorder.session_id)
        a, b = db.get_trace(laps[1]["id"]), db.get_trace(laps[2]["id"])
        result = analysis.compare(a, b)
        self.assertEqual(len(result["d"]), len(result["delta"]))
        self.assertAlmostEqual(result["delta"][-1], laps[1]["lap_time"] - laps[2]["lap_time"], delta=0.05)

    def test_stats(self):
        stats = analysis.session_stats(db.list_laps(self.recorder.session_id))
        self.assertLessEqual(stats["ideal_lap"], stats["best_lap"])
        self.assertIsNotNone(stats["stdev"])

    def test_api_and_delete(self):
        sid = self.recorder.session_id
        self.assertEqual(self.client.get(f"/api/sessions/{sid}").status_code, 200)
        # la session en cours d'enregistrement est protégée
        self.assertEqual(self.client.delete(f"/api/sessions/{sid}").status_code, 409)
        other = db.create_session("Autre", 1000.0, "Voiture", "Race")
        db.insert_lap(other, {"lap_number": 1, "lap_time": 60.0, "sectors": None, "fuel_used": None,
                              "fuel_left": None, "max_speed_kmh": 100.0, "throttle_avg": 0.5,
                              "brake_avg": 0.1, "flag": None}, {"t": [0], "d": [0]})
        self.assertEqual(self.client.delete(f"/api/sessions/{other}").status_code, 204)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM laps WHERE session_id = ?", (other,))[0]["n"], 0)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM traces")[0]["n"],
                         db.query("SELECT COUNT(*) AS n FROM laps")[0]["n"])


class AnalysisFeaturesTest(RecorderTest):
    """Réutilise les tours simulés de RecorderTest."""

    def test_corners_map_and_advice(self):
        laps = db.list_laps(self.recorder.session_id)
        a, b = db.get_trace(laps[1]["id"]), db.get_trace(laps[2]["id"])
        result = analysis.compare(a, b)
        corners = analysis.corner_analysis(result, 4000.0)
        self.assertEqual(len(corners), 6)  # le circuit de démo a 6 virages
        self.assertAlmostEqual(sum(c["time_lost"] for c in corners), result["delta"][-1], delta=0.5)
        track = analysis.track_map(b, result["d"])
        # la carte reconstituée fait à peu près la taille d'un circuit de 4 km
        width = max(track["x"]) - min(track["x"])
        self.assertTrue(300 < width < 2500, width)
        self.assertAlmostEqual(track["x"][0], track["x"][-1], delta=30)

    def test_compare_endpoint_with_record(self):
        sid = self.recorder.session_id
        detail = self.client.get(f"/api/sessions/{sid}").get_json()
        self.assertIsNotNone(detail["record"])
        lap_id = detail["laps"][-1]["id"]
        data = self.client.get(f"/api/compare?lap={lap_id}&ref={detail['record']['id']}").get_json()
        self.assertIn("corners", data)
        self.assertIsNotNone(data["map"])
        self.assertEqual(len(data["engineer"]["profile"]), 4)

    def test_live_delta_against_record(self):
        self.assertIsNotNone(self.recorder.record)
        drive(self.recorder, self.fake, 30)
        status = self.recorder.status()
        self.assertIsNotNone(status["delta"])
        self.assertLess(abs(status["delta"]), 5)

    def test_debrief_uses_corner_telemetry(self):
        import time
        start = time.perf_counter()
        result = self.client.get(f"/api/sessions/{self.recorder.session_id}/debrief").get_json()
        self.assertTrue(result["ready"])
        self.assertLess(time.perf_counter() - start, 10)
        self.assertTrue(result["good"] or result["bad"])

    def test_rename_and_record_history(self):
        sid = self.recorder.session_id
        self.assertEqual(self.client.patch(f"/api/sessions/{sid}", json={"name": "Test setup", "note": "moins d'appui"}).status_code, 204)
        session = db.query_one("SELECT * FROM sessions WHERE id = ?", (sid,))
        self.assertEqual((session["name"], session["note"]), ("Test setup", "moins d'appui"))
        history = self.client.get("/api/records?car=Voiture de démo&track=Circuit de démo").get_json()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["record"], history[0]["session_best"])


class CoachingTest(unittest.TestCase):
    """Deux sessions simulées sur le même circuit : entraînement, voix, comparaison, progression."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(cls.tmp.name) / "sessions.db"
        db.init_db()
        db.set_setting("voice", "laps")
        cls.spoken = []
        cls._say = telemetry.speaker.say
        telemetry.speaker.say = cls.spoken.append
        cls.sessions = []
        for seed in (3, 4):
            fake = FakeIRSDK(seed=seed)
            recorder = telemetry.TelemetryRecorder(ir=fake, waiter=DemoWaiter(fake, 0))
            if seed == 4:
                # entraînement sur le 3e virage du circuit de démo (≈ 1700 m sur 4000 m)
                recorder._check_connection()
                recorder._tick()
                recorder.set_training(recorder.car, recorder.track, 3, 1500 / 4000, 1850 / 4000)
            drive(recorder, fake, 5 * 85)
            cls.sessions.append(recorder.session_id)
            cls.recorder = recorder
        main.recorder = cls.recorder
        cls.client = main.app.test_client()

    @classmethod
    def tearDownClass(cls):
        telemetry.speaker.say = cls._say
        cls.tmp.cleanup()

    def test_training_times_each_pass(self):
        tr = self.recorder.status()["training"]
        self.assertTrue(tr["active"])
        self.assertGreaterEqual(tr["count"], 4)
        self.assertIsNotNone(tr["ref_time"])
        # la référence (record de la 1re session) est chargée dès le début de la 2e session
        self.assertTrue(all(a["delta"] is not None for a in tr["attempts"]))
        for attempt in tr["attempts"][1:]:  # le 1er passage est dans l'out-lap (limiteur de stand)
            self.assertLess(abs(attempt["delta"]), 1.0)
        self.assertTrue(any(t.startswith("Virage 3, ") for t in self.spoken))

    def test_live_delta_available_from_session_start(self):
        self.assertIsNotNone(self.recorder.record)

    def test_spoken_formats(self):
        from voice import spoken_delta, spoken_time
        self.assertEqual(spoken_time(82.43), "1 22 4")
        self.assertEqual(spoken_delta(-0.34), "moins 0 virgule 3")
        self.assertEqual(spoken_delta(0.04), "plus 4 centièmes")
        self.assertEqual(spoken_delta(-0.004), "moins 1 centième")

    def test_lap_announcements(self):
        self.assertTrue(any(t.startswith("Record battu") or " virgule " in t for t in self.spoken), self.spoken)

    def test_settings_api(self):
        self.assertEqual(self.client.put("/api/settings", json={"voice": "off"}).get_json()["voice"], "off")
        self.assertEqual(self.client.put("/api/settings", json={"voice": "nimporte"}).status_code, 400)
        self.client.put("/api/settings", json={"voice": "laps"})
        res = self.client.put("/api/settings", json={"voice_rate": "lent", "voice_volume": 80, "overlay": "off"}).get_json()
        self.assertEqual((res["voice_rate"], res["voice_volume"], res["overlay"]), ("lent", "80", "off"))
        self.assertEqual((telemetry.speaker.rate, telemetry.speaker.volume), (-2, 80))
        self.assertEqual(self.client.put("/api/settings", json={"voice_volume": 150}).status_code, 400)
        self.assertEqual(self.client.put("/api/settings", json={"voice_rate": "turbo"}).status_code, 400)
        self.assertEqual(self.client.post("/api/voice-test").status_code, 204)
        self.client.put("/api/settings", json={"voice_rate": "normal", "voice_volume": 100, "overlay": "on"})

    def test_compare_sessions(self):
        a, b = self.sessions
        data = self.client.get(f"/api/compare-sessions?a={a}&b={b}").get_json()
        self.assertTrue(data["same_track"])
        self.assertEqual(len(data["sectors"]), 10)
        self.assertEqual(len(data["corners"]), 6)
        self.assertTrue(any("Meilleur tour" in v["text"] for v in data["verdict"]))

    def test_progress_debrief(self):
        data = self.client.get("/api/progress-debrief?car=Voiture de démo&track=Circuit de démo").get_json()
        self.assertTrue(data["ready"], data)
        self.assertEqual(data["period"]["sessions"], 2)

    def test_training_api(self):
        res = self.client.post("/api/training", json={"car": "X", "track": "Y", "number": 1, "d0": 0.5, "d1": 0.2})
        self.assertEqual(res.status_code, 400)


class TechniqueTest(unittest.TestCase):
    """Le simulateur de démo tire au sort des défauts de pilotage : l'analyse doit les retrouver."""

    def test_detects_simulated_driving_errors(self):
        tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(tmp.name) / "sessions.db"
        db.init_db()
        fake = FakeIRSDK(seed=7)
        recorder = telemetry.TelemetryRecorder(ir=fake, waiter=DemoWaiter(fake, 0))
        waiter, styles = DemoWaiter(fake, 0), {}
        for _ in range(60 * 85 * 8):
            waiter.wait()
            styles[fake.lap] = (fake.style, fake.shift_factor)
            recorder._check_connection()
            recorder._tick()
        laps = [l for l in db.list_laps(recorder.session_id) if l["lap_time"] and not l["flag"]]
        best = min(laps, key=lambda l: l["lap_time"])
        meta = db.lap_meta(best["id"])
        ref_style = styles[best["lap_number"]][0]
        missed = extra = 0
        for lap in laps:
            if lap["id"] == best["id"]:
                continue
            result = analysis.compare(db.get_trace(lap["id"]), db.get_trace(best["id"]))
            report = technique.lap_report(result, meta)
            self.assertEqual(report["missing"], [])
            got = {(r["corner"], r["code"]) for r in report["remarks"]}
            style, shift_factor = styles[lap["lap_number"]]
            expected = {(None, "early_shift")} if shift_factor < 1 else set()
            for i, (mine, ref) in enumerate(zip(style, ref_style), start=1):
                if mine["brake_ramp"] > 0.1 and ref["brake_ramp"] < 0.1:
                    expected.add((i, "brake_attack"))
                if mine["throttle_lift"]:
                    expected.add((i, "throttle_lift"))
                if mine["long_gear"] and not ref["long_gear"]:
                    expected.add((i, "long_gear"))
            missed += len(expected - got)
            extra += len(got - expected)
        tmp.cleanup()
        self.assertEqual((missed, extra), (0, 0))


class HabitsTest(unittest.TestCase):
    def test_habits_mix_corner_and_whole_lap_errors(self):
        def remark(corner, code):
            return {"corner": corner, "code": code, "topic": "Rapports", "observation": "o.", "action": "a.",
                    "consequence": "c.", "severity": 2}
        reports = [{"remarks": [remark(None, "early_shift"), remark(3, "long_gear")], "missing": []}] * 4
        good, bad = [], []
        debrief._habits(reports, good, bad)  # plantait : tri entre None et un numéro de virage
        self.assertEqual(len(bad), 2)
        self.assertIn("sur tout le tour", " ".join(r["title"] for r in bad))


class RemarksTest(unittest.TestCase):
    """Formulations variées, mémoire d'une session à l'autre, résumé radio."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(cls.tmp.name) / "sessions.db"
        db.init_db()
        db.set_setting("voice", "records")
        cls.spoken = []
        cls._say = telemetry.speaker.say
        telemetry.speaker.say = cls.spoken.append
        cls.sessions = []
        for seed in (7, 7):  # deux sessions avec les mêmes défauts (même graine)
            fake = FakeIRSDK(seed=seed)
            recorder = telemetry.TelemetryRecorder(ir=fake, waiter=DemoWaiter(fake, 0))
            drive(recorder, fake, 8 * 85)
            fake.on_track = False  # retour au garage : résumé radio
            drive(recorder, fake, 3)
            cls.sessions.append(recorder.session_id)
        cls.client = main.app.test_client()
        main.recorder = recorder

    @classmethod
    def tearDownClass(cls):
        telemetry.speaker.say = cls._say
        cls.tmp.cleanup()

    def test_phrasing_is_varied_but_stable(self):
        rng_a, rng_b = technique.random.Random("tour-1"), technique.random.Random("tour-1")
        self.assertEqual(technique._phrase("brake_attack", rng_a, attack="0,3", ref_txt=""),
                         technique._phrase("brake_attack", rng_b, attack="0,3", ref_txt=""))
        seen = {technique._phrase("brake_attack", technique.random.Random(f"tour-{i}"), attack="0,3", ref_txt="")[0]
                for i in range(30)}
        self.assertGreaterEqual(len(seen), 3)

    def test_habits_remembered_across_sessions(self):
        first, second = self.sessions
        debrief.session_debrief(second)  # analyse aussi la session précédente si besoin
        self.assertTrue(db.has_habit_run(first))
        result = debrief.session_debrief(second)
        details = " ".join(r["detail"] for r in result["bad"])
        self.assertIn("2e session d'affilée", details)

    def test_lap_report_mentions_history(self):
        laps = [l for l in db.list_laps(self.sessions[1]) if l["lap_time"] and not l["flag"]]
        best = min(laps, key=lambda l: l["lap_time"])
        other = next(l for l in laps if l["id"] != best["id"])
        data = self.client.get(f"/api/compare?lap={other['id']}&ref={best['id']}").get_json()
        engineer = data["engineer"]
        self.assertIn("improved", engineer)
        self.assertTrue(all("history" in r for r in engineer["remarks"]))

    def test_radio_summary(self):
        text = debrief.radio_summary(self.sessions[0])
        self.assertTrue(text.startswith("Fin de relais. Meilleur tour 1 2"), text)
        self.assertNotIn("±", text)
        self.assertTrue(any(t.startswith("Fin de relais") for t in self.spoken), self.spoken)
        res = self.client.post(f"/api/sessions/{self.sessions[0]}/radio").get_json()
        self.assertEqual(res["text"], text)

    def test_speakable(self):
        self.assertEqual(debrief.speakable("Rythme irrégulier : ± 0,952 s"), "Rythme irrégulier : plus ou moins 0,952 secondes")
        self.assertEqual(debrief.speakable("Secteur S5 irrégulier"), "Secteur 5 irrégulier")
        self.assertEqual(debrief.speakable("Ta régularité en S5 stagne"), "Ta régularité en secteur 5 stagne")
        self.assertEqual(debrief.speakable("Virage 3 : −0,26 s par tour"), "Virage 3 : moins 0,26 secondes par tour")


class DebriefTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "sessions.db"
        db.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def add_session(self, times, flags=None):
        sid = db.create_session("Spa", 7000.0, "MX-5", "Practice")
        for i, t in enumerate(times, start=1):
            db.insert_lap(sid, {"lap_number": i, "lap_time": t, "sectors": None, "fuel_used": 2.0,
                                "fuel_left": 20.0, "max_speed_kmh": 200.0, "throttle_avg": 0.6,
                                "brake_avg": 0.1, "flag": (flags or {}).get(i)}, None)
        return sid

    def titles(self, items):
        return " | ".join(r["title"] for r in items)

    def test_not_enough_laps(self):
        sid = self.add_session([150.0, 151.0])
        self.assertFalse(debrief.session_debrief(sid)["ready"])

    def test_good_and_bad_points(self):
        self.add_session([150.0, 149.5, 149.8, 150.1, 149.9])  # session précédente : record 149.5
        steady = self.add_session([149.2, 149.3, 149.25, 149.3, 149.2, 149.28, 149.3, 149.22, 149.25, 149.3])
        result = debrief.session_debrief(steady)
        self.assertIn("Nouveau record", self.titles(result["good"]))
        self.assertIn("Très régulier", self.titles(result["good"]))

        messy = self.add_session([151.0, 153.5, 150.9, None, 152.8, 151.2, 154.0, 151.5, 152.9, 153.6])
        result = debrief.session_debrief(messy)
        bad = self.titles(result["bad"])
        self.assertIn("Rythme irrégulier", bad)
        self.assertIn("sans temps valide", bad)
        self.assertIn("de ton record", bad)
        self.assertIsNotNone(result["priority"])
        self.assertIn("Rythme irrégulier", result["priority"]["title"])


class LapDeleteAndBackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "sessions.db"
        db.init_db()
        fake = FakeIRSDK(seed=1)
        main.recorder = telemetry.TelemetryRecorder(ir=fake, waiter=DemoWaiter(fake, 0))  # jamais démarré
        self.client = main.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def make_session(self, session_type):
        sid = db.create_session("Spa", 7000.0, "MX-5", session_type)
        lap_id = db.insert_lap(sid, {"lap_number": 1, "lap_time": 150.0, "sectors": None, "fuel_used": 2.0,
                                     "fuel_left": 20.0, "max_speed_kmh": 200.0, "throttle_avg": 0.6,
                                     "brake_avg": 0.1, "flag": None}, {"t": [0, 1], "d": [0, 1]})
        return sid, lap_id

    def test_lap_delete_only_in_race(self):
        _, practice_lap = self.make_session("Practice")
        _, race_lap = self.make_session("Race")
        self.assertEqual(self.client.delete(f"/api/laps/{practice_lap}").status_code, 403)
        self.assertEqual(self.client.delete(f"/api/laps/{race_lap}").status_code, 204)
        self.assertIsNone(db.get_lap(race_lap))
        self.assertIsNone(db.get_trace(race_lap))
        self.assertIsNotNone(db.get_lap(practice_lap))

    def test_backup_and_restore(self):
        self.make_session("Practice")
        backup = self.client.get("/api/backup")
        self.assertEqual(backup.status_code, 200)
        content = backup.data
        backup.close()
        self.make_session("Race")  # modifié après la sauvegarde
        self.assertEqual(len(db.list_sessions()), 2)
        import io
        res = self.client.post("/api/restore", data={"file": (io.BytesIO(content), "sauvegarde.db")},
                               content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(len(db.list_sessions()), 1)
        self.assertTrue((db.DB_PATH.parent / res.get_json()["safety_copy"]).exists())
        bad = self.client.post("/api/restore", data={"file": (io.BytesIO(b"pas une base"), "x.db")},
                               content_type="multipart/form-data")
        self.assertEqual(bad.status_code, 400)


class ObjectivesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "sessions.db"
        db.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def add_session(self, car, track, times):
        sid = db.create_session(track, 5000.0, car, "Practice")
        for i, t in enumerate(times, start=1):
            db.insert_lap(sid, {"lap_number": i, "lap_time": t, "sectors": None, "fuel_used": 2.0,
                                "fuel_left": 20.0, "max_speed_kmh": 200.0, "throttle_avg": 0.6,
                                "brake_avg": 0.1, "flag": None}, None)
        return sid

    def test_new_car_and_track_get_replicated_objectives(self):
        objectives.ensure_car("MX-5", "Spa")
        objectives.ensure_car("GR86", "Spa")
        for car in ("MX-5", "GR86"):
            self.assertEqual(len(objectives.list_objectives(car)["objectives"]), len(objectives.CAR_TEMPLATES))
            self.assertEqual(len(objectives.list_objectives(car, "Spa")["objectives"]), len(objectives.TRACK_TEMPLATES))
        self.assertGreaterEqual(len(objectives.CAR_TEMPLATES) + len(objectives.TRACK_TEMPLATES), 100)

    def test_lap_targets_are_relative_to_own_reference(self):
        self.add_session("MX-5", "Spa", [150.0, 149.0, 148.0, 146.0, 145.5])
        objectives.evaluate("MX-5", "Spa")
        data = objectives.list_objectives("MX-5", "Spa")
        self.assertEqual(data["baseline"], 148.0)
        pb = {o["code"]: o for o in data["objectives"]}
        self.assertIsNotNone(pb["t_pb_1"]["completed_at"])       # 145.5 < 148 × 0,99
        self.assertIsNone(pb["t_pb_2"]["completed_at"])          # 145.5 > 148 × 0,98
        self.assertAlmostEqual(pb["t_pb_2"]["target_time"], 145.04, places=2)

    def test_xp_and_levels(self):
        self.add_session("MX-5", "Spa", [100.0] * 12)
        unlocked = objectives.evaluate("MX-5", "Spa")
        self.assertIn("Boucler 1 tour", unlocked)
        profile = objectives.profile()
        self.assertGreater(profile["xp"], 0)
        self.assertGreater(profile["level"], 1)
        # une seconde évaluation ne redonne pas d'XP
        self.assertEqual(objectives.evaluate("MX-5", "Spa"), [])
        self.assertEqual(objectives.profile()["xp"], profile["xp"])


if __name__ == "__main__":
    unittest.main()
