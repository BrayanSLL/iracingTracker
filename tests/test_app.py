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
import main  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
