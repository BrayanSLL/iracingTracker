"""Tests de la lecture Le Mans Ultimate sans le jeu : la voiture de démo écrit dans une fausse
mémoire partagée LMU, avec la même disposition que celle du jeu.

    python -m unittest discover tests
"""
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import lmu  # noqa: E402
import telemetry  # noqa: E402
from demo import DT, TRACK_LENGTH, FakeIRSDK, heading  # noqa: E402

SCORING_EVERY = 12  # le classement LMU n'est mis à jour que 5 fois par seconde


class FakeLMUGame:
    """Fait rouler la voiture de démo et publie son état comme le ferait Le Mans Ultimate."""

    def __init__(self):
        self.fake = FakeIRSDK(seed=7)
        self.fake.startup()
        self.memory = bytearray(lmu.ctypes.sizeof(lmu.SharedData))
        self.view = lmu.SharedData.from_buffer(self.memory)
        self.view.generic.gameVersion = 1
        self.view.telemetry.playerHasVehicle = True
        self.view.telemetry.activeVehicles = 1
        info = self.view.scoring.scoringInfo
        info.mTrackName = b"Circuit LMU"
        info.mSession = 11  # course
        info.mLapDist = TRACK_LENGTH
        info.mNumVehicles = 2
        self.view.scoring.vehScoringInfo[1].mIsPlayer = True  # la voiture du joueur n'est pas forcément la 1re
        self.view.telemetry.playerVehicleIdx = 1
        self.telem = self.view.telemetry.telemInfo[1]
        self.telem.mVehicleModel = b"Voiture LMU"
        self.telem.mVehicleName = b"#7 Equipe - Voiture LMU"
        self.telem.mPhysicalSteeringWheelRange = 540.0
        self.telem.mEngineMaxRPM = 7500.0
        self.steps = 0
        self.x = self.z = 0.0

    def step(self):
        fake = self.fake
        fake.step()
        self.steps += 1
        values = {key: fake[key] for key in ("Speed", "YawNorth", "Throttle", "Brake", "Gear", "FuelLevel", "RPM",
                                             "SteeringWheelAngle", "LapLastLapTime", "LapDistPct", "Lap",
                                             "SessionTime", "IsOnTrack", "OnPitRoad")}
        telem = self.telem
        telem.mElapsedTime = values["SessionTime"]
        telem.mLapNumber = values["Lap"]
        telem.mLapStartET = fake.lap_start_time
        telem.mLocalVel.z = -values["Speed"]  # la voiture avance vers -z dans son repère
        self.x += values["Speed"] * DT * math.sin(values["YawNorth"])
        self.z += values["Speed"] * DT * math.cos(values["YawNorth"])
        telem.mPos.x, telem.mPos.z = self.x, self.z
        telem.mUnfilteredThrottle = values["Throttle"]
        telem.mUnfilteredBrake = values["Brake"]
        telem.mUnfilteredSteering = -values["SteeringWheelAngle"] / math.radians(270)
        telem.mGear = values["Gear"]
        telem.mFuel = values["FuelLevel"]
        telem.mEngineRPM = values["RPM"]
        info = self.view.scoring.scoringInfo
        info.mInRealtime = values["IsOnTrack"]
        if self.steps % SCORING_EVERY == 0:
            info.mCurrentET = values["SessionTime"]
            vehicle = self.view.scoring.vehScoringInfo[1]
            vehicle.mLapDist = values["LapDistPct"] * TRACK_LENGTH
            vehicle.mLastLapTime = values["LapLastLapTime"]
            vehicle.mInPits = values["OnPitRoad"]


class GameWaiter:
    def __init__(self, game):
        self.game = game

    def wait(self):
        self.game.step()

    def reset(self):
        pass


class LMUTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(cls.tmp.name) / "sessions.db"
        db.init_db()
        cls.game = FakeLMUGame()
        cls.sdk = lmu.LMUSDK(opener=lambda: cls.game.memory, is_running=lambda: True)
        cls.recorder = telemetry.TelemetryRecorder(sources=[(cls.sdk, GameWaiter(cls.game), lmu.LMUSDK.name)])
        for _ in range(int(3 * 85 * 60)):  # ~3 tours
            cls.recorder.waiter.wait()
            if cls.recorder._check_connection():
                cls.recorder._tick()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_session_is_detected(self):
        status = self.recorder.status()
        self.assertTrue(status["connected"])
        self.assertEqual(status["sim"], "Le Mans Ultimate")
        self.assertEqual((status["track"], status["car"]), ("Circuit LMU", "Voiture LMU"))
        session = next(s for s in db.list_sessions() if s["id"] == self.recorder.session_id)
        self.assertEqual(session["session_type"], "Race")
        self.assertAlmostEqual(session["track_length_m"], TRACK_LENGTH)
        self.assertTrue(db.is_race(session))

    def test_laps_have_official_times_and_full_traces(self):
        laps = db.list_laps(self.recorder.session_id)
        self.assertGreaterEqual(len(laps), 2)
        self.assertEqual(laps[0]["flag"], "pit")  # sortie des stands
        for lap in laps[1:]:
            self.assertIsNone(lap["flag"])
            self.assertTrue(75 < lap["lap_time"] < 95, lap["lap_time"])
            trace = db.get_trace(lap["id"])
            self.assertGreater(len(trace["d"]), 60 * 70)
            self.assertEqual(trace["d"], sorted(trace["d"]))
            self.assertGreater(trace["d"][-1], 0.98)  # distance interpolée entre deux mises à jour du classement
            self.assertGreater(max(trace["throttle"]), 0.9)
            self.assertGreater(max(trace["brake"]), 0.5)

    def test_heading_matches_the_car(self):
        # le cap est recalculé depuis les positions x/z du jeu : il doit suivre celui de la voiture
        # (un axe inversé donnerait une carte du circuit en miroir)
        trace = db.get_trace(db.list_laps(self.recorder.session_id)[1]["id"])
        errors = sorted(abs((yaw - heading(d * TRACK_LENGTH) + math.pi) % (2 * math.pi) - math.pi)
                        for yaw, d in zip(trace["yaw"], trace["d"]))
        self.assertLess(errors[len(errors) // 2], 0.05)

    def test_live_pedals(self):
        data = self.recorder.inputs_since(0)
        self.assertEqual(data["gear"], self.game.fake["Gear"])
        self.assertAlmostEqual(data["throttle"], self.game.fake["Throttle"])
        self.assertTrue(data["samples"])

    def test_invalid_lap_has_no_time(self):
        sdk = lmu.LMUSDK(opener=lambda: self.game.memory, is_running=lambda: True)
        sdk.startup()
        telem = self.game.telem
        lap = telem.mLapNumber
        telem.mLapInvalidated = True
        sdk.freeze_var_buffer_latest()
        telem.mLapNumber = lap + 1
        telem.mLapInvalidated = False
        sdk.freeze_var_buffer_latest()
        self.assertEqual(sdk["LapLastLapTime"], -1.0)
        telem.mLapNumber = lap

    def test_not_running(self):
        sdk = lmu.LMUSDK(opener=lambda: None)
        self.assertFalse(sdk.startup())
        self.assertFalse(sdk.is_connected)
        closed = lmu.LMUSDK(opener=lambda: self.game.memory, is_running=lambda: False)
        closed.startup()
        closed._checked_at = -100
        self.assertFalse(closed.is_connected)


if __name__ == "__main__":
    unittest.main()
