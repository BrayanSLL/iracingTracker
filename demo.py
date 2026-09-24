"""Faux iRacing pour essayer l'application (et la tester) sans lancer le simulateur.

Simule une voiture qui tourne sur un circuit de 4 km : vitesse, gaz, frein, rapports,
carburant, temps au tour, sortie des stands… avec de petites variations d'un tour à l'autre.
"""
import math
import random
import time

TRACK_LENGTH = 4000.0  # mètres
DT = 1 / 60
V_MAX = 78.0           # m/s
ACCEL = 6.5            # m/s² en accélération
DECEL = 13.0           # m/s² au freinage
PIT_EXIT = 250.0       # la sortie des stands se termine à 250 m
LAP_TIME_LAG = 6       # ticks avant que LapLastLapTime soit à jour (comme le vrai iRacing)

# (position du virage en m, vitesse mini en m/s, sens)
CORNERS = [(450, 24, 1), (1150, 38, -1), (1700, 18, 1), (2350, 45, -1), (2900, 30, 1), (3600, 22, -1)]


def speed_profile(corner_speeds, brake_offsets):
    """Vitesse cible tous les mètres, limitée par l'accélération et le freinage."""
    n = int(TRACK_LENGTH)
    limit = [V_MAX] * n
    for (pos, _, _), v_min, offset in zip(CORNERS, corner_speeds, brake_offsets):
        for dx in range(-40, 41):  # un virage fait ~80 m
            limit[(pos + dx) % n] = min(limit[(pos + dx) % n], v_min + abs(dx) * 0.04)
        limit[(pos - int(offset)) % n] = min(limit[(pos - int(offset)) % n], limit[pos % n] + 0.1)
    v = limit[:]
    for _ in range(2):  # deux passes pour gérer le bouclage du tour
        for i in range(1, 2 * n):
            a, b = (i - 1) % n, i % n
            v[b] = min(v[b], math.sqrt(v[a] ** 2 + 2 * ACCEL))
        for i in range(2 * n, 0, -1):
            a, b = i % n, (i - 1) % n
            v[b] = min(v[b], math.sqrt(v[a] ** 2 + 2 * DECEL))
    return v


class FakeIRSDK:
    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.is_initialized = False
        self.is_connected = False
        self.session_time = 0.0
        self.tick = 0
        self.dist = 20.0  # départ dans la pitlane, juste après la ligne : premier tour = out-lap
        self.lap = 1
        self.lap_completed = 0
        self.lap_start_time = 0.0
        self.last_lap_time = -1.0
        self.pending_lap_time = None
        self.fuel = 45.0
        self.speed = 15.0
        self.gear = 1
        self.throttle = 0.0
        self.brake = 0.0
        self.steer = 0.0
        self.pit_road = True
        self._new_lap_profile()

    def _new_lap_profile(self):
        mistake = self.rng.random() < 0.15  # parfois un virage raté
        speeds, offsets = [], []
        for i, (_, v_min, _) in enumerate(CORNERS):
            factor = self.rng.gauss(1.0, 0.015)
            if mistake and i == self.rng.randrange(len(CORNERS)):
                factor -= 0.18
            speeds.append(v_min * factor)
            offsets.append(self.rng.uniform(0, 12))
        self.profile = speed_profile(speeds, offsets)

    # --- API pyirsdk utilisée par telemetry.py --------------------------------

    def startup(self):
        self.is_initialized = self.is_connected = True
        return True

    def shutdown(self):
        self.is_initialized = self.is_connected = False

    def freeze_var_buffer_latest(self):
        pass

    def unfreeze_var_buffer_latest(self):
        pass

    def __getitem__(self, key):
        values = {
            "SessionTick": self.tick,
            "SessionNum": 0,
            "SessionTime": self.session_time,
            "IsOnTrack": True,
            "OnPitRoad": self.pit_road,
            "Lap": self.lap,
            "LapCompleted": self.lap_completed,
            "LapDistPct": self.dist / TRACK_LENGTH,
            "LapLastLapTime": self.last_lap_time,
            "LapCurrentLapTime": self.session_time - self.lap_start_time,
            "Speed": self.speed,
            "Throttle": self.throttle,
            "Brake": self.brake,
            "Gear": self.gear,
            "SteeringWheelAngle": self.steer,
            "FuelLevel": self.fuel,
            "WeekendInfo": {"TrackDisplayName": "Circuit de démo", "TrackLength": "4.00 km"},
            "DriverInfo": {"DriverCarIdx": 0, "Drivers": [{"CarIdx": 0, "CarScreenName": "Voiture de démo"}]},
            "SessionInfo": {"Sessions": [{"SessionNum": 0, "SessionType": "Practice"}]},
        }
        return values.get(key)

    # --- simulation ------------------------------------------------------------

    def step(self):
        self.tick += 1
        self.session_time += DT

        n = int(TRACK_LENGTH)
        i = int(self.dist) % n
        target = self.profile[i]
        ahead = self.profile[(i + 3) % n]
        if self.pit_road:
            target = ahead = min(target, 22.0)  # limiteur de vitesse dans les stands
        self.speed += max(-DECEL * DT, min(ACCEL * DT, target - self.speed))
        if ahead < target - 0.05:  # zone de freinage
            self.throttle = 0.0
            self.brake = min(1.0, 0.55 + (target - ahead) / 3) * self.rng.uniform(0.97, 1.0)
        elif ahead > target + 0.05:  # accélération
            self.throttle = 1.0
            self.brake = 0.0
        else:  # vitesse stabilisée (corde, ligne droite en butée)
            self.throttle = 1.0 if target >= V_MAX - 0.5 else 0.3 + self.rng.uniform(0, 0.05)
            self.brake = 0.0
        self.gear = min(6, 1 + int(self.speed / 14))
        self.steer = sum(direction * 1.2 * math.exp(-((self.dist - pos) / 45) ** 2)
                         for pos, _, direction in CORNERS)
        self.fuel -= 0.00045 + 0.0012 * self.throttle
        if self.fuel < 2.0:
            self.fuel = 45.0  # ravitaillement « magique » pour que la démo tourne indéfiniment

        if self.pending_lap_time is not None:
            self.pending_lap_time[1] -= 1
            if self.pending_lap_time[1] <= 0:
                self.last_lap_time = self.pending_lap_time[0]
                self.pending_lap_time = None

        previous = self.dist
        self.dist += self.speed * DT
        if self.pit_road and self.dist < TRACK_LENGTH and previous < PIT_EXIT <= self.dist:
            self.pit_road = False
        if self.dist >= TRACK_LENGTH:  # passage de ligne
            overshoot = (self.dist - TRACK_LENGTH) / max(self.speed, 1e-6)
            crossing = self.session_time - overshoot
            self.pending_lap_time = [round(crossing - self.lap_start_time, 3), LAP_TIME_LAG]
            self.lap_start_time = crossing
            self.dist -= TRACK_LENGTH
            self.lap += 1
            self.lap_completed += 1
            self._new_lap_profile()


class DemoWaiter:
    """Remplace l'attente de l'événement iRacing : fait avancer la simulation d'un tick."""

    def __init__(self, fake, speedup=1.0):
        self.fake = fake
        self.speedup = speedup

    def wait(self):
        if self.speedup > 0:
            time.sleep(DT / self.speedup)
        self.fake.step()

    def reset(self):
        pass
