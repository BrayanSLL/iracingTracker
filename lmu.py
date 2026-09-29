"""Lecture de Le Mans Ultimate (mémoire partagée intégrée au jeu, `LMU_Data`).

LMU publie sa télémétrie dans une mémoire partagée décrite par l'en-tête
`Support/SharedMemoryInterface` fourni avec le jeu (structures héritées de rFactor 2).
Ce module la relit et la présente comme pyirsdk (`ir["Speed"]`, `ir["LapDistPct"]`…) :
le reste de l'application (enregistrement des tours, analyse, interface) est le même
pour les deux simulateurs.

Rien à installer ni à activer dans le jeu. Seul Windows est pris en charge (comme iRacing).
"""
import ctypes
import math
import time

SHARED_MEMORY_NAME = "LMU_Data"
PROCESS_NAME = "Le Mans Ultimate.exe"
MAX_VEHICLES = 104
MAX_PATH = 260
TICK = 0.004  # attente entre deux lectures (le jeu met à jour la télémétrie jusqu'à 100 fois par seconde)
PROCESS_CHECK = 2.0  # secondes entre deux vérifications que le jeu tourne toujours
DEFAULT_WHEEL_RANGE = 540.0  # degrés, si le jeu n'indique pas la rotation du volant

c_double, c_float, c_int, c_uint, c_short = ctypes.c_double, ctypes.c_float, ctypes.c_int, ctypes.c_uint, ctypes.c_short
c_ubyte, c_byte, c_bool, c_char, c_ushort = ctypes.c_ubyte, ctypes.c_byte, ctypes.c_bool, ctypes.c_char, ctypes.c_ushort
c_ulonglong = ctypes.c_ulonglong


def _struct(name, fields):
    """Structure C alignée sur 4 octets (#pragma pack(4) de l'en-tête du jeu)."""
    return type(name, (ctypes.Structure,), {"_pack_": 4, "_fields_": fields})


# --- structures (ordre et types identiques à l'en-tête du jeu : ne pas modifier) ------------------

Vect3 = _struct("Vect3", [("x", c_double), ("y", c_double), ("z", c_double)])

Wheel = _struct("Wheel", [
    ("mSuspensionDeflection", c_double), ("mRideHeight", c_double), ("mSuspForce", c_double),
    ("mBrakeTemp", c_double), ("mBrakePressure", c_double), ("mRotation", c_double),
    ("mLateralPatchVel", c_double), ("mLongitudinalPatchVel", c_double), ("mLateralGroundVel", c_double),
    ("mLongitudinalGroundVel", c_double), ("mCamber", c_double), ("mLateralForce", c_double),
    ("mLongitudinalForce", c_double), ("mTireLoad", c_double), ("mGripFract", c_double), ("mPressure", c_double),
    ("mTemperature", c_double * 3), ("mWear", c_double), ("mTerrainName", c_char * 16), ("mSurfaceType", c_ubyte),
    ("mFlat", c_bool), ("mDetached", c_bool), ("mStaticUndeflectedRadius", c_ubyte),
    ("mVerticalTireDeflection", c_double), ("mWheelYLocation", c_double), ("mToe", c_double),
    ("mTireCarcassTemperature", c_double), ("mTireInnerLayerTemperature", c_double * 3), ("mOptimalTemp", c_float),
    ("mCompoundIndex", c_ubyte), ("mCompoundType", c_ubyte), ("mExpansion", c_ubyte * 18),
])

VehicleTelemetry = _struct("VehicleTelemetry", [
    ("mID", c_int), ("mDeltaTime", c_double), ("mElapsedTime", c_double), ("mLapNumber", c_int),
    ("mLapStartET", c_double), ("mVehicleName", c_char * 64), ("mTrackName", c_char * 64),
    ("mPos", Vect3), ("mLocalVel", Vect3), ("mLocalAccel", Vect3), ("mOri", Vect3 * 3), ("mLocalRot", Vect3),
    ("mLocalRotAccel", Vect3), ("mGear", c_int), ("mEngineRPM", c_double), ("mEngineWaterTemp", c_double),
    ("mEngineOilTemp", c_double), ("mClutchRPM", c_double),
    ("mUnfilteredThrottle", c_double), ("mUnfilteredBrake", c_double), ("mUnfilteredSteering", c_double),
    ("mUnfilteredClutch", c_double), ("mFilteredThrottle", c_double), ("mFilteredBrake", c_double),
    ("mFilteredSteering", c_double), ("mFilteredClutch", c_double), ("mSteeringShaftTorque", c_double),
    ("mFront3rdDeflection", c_double), ("mRear3rdDeflection", c_double), ("mFrontWingHeight", c_double),
    ("mFrontRideHeight", c_double), ("mRearRideHeight", c_double), ("mDrag", c_double),
    ("mFrontDownforce", c_double), ("mRearDownforce", c_double), ("mFuel", c_double), ("mEngineMaxRPM", c_double),
    ("mScheduledStops", c_ubyte), ("mOverheating", c_bool), ("mDetached", c_bool), ("mHeadlights", c_bool),
    ("mDentSeverity", c_ubyte * 8), ("mLastImpactET", c_double), ("mLastImpactMagnitude", c_double),
    ("mLastImpactPos", Vect3), ("mEngineTorque", c_double), ("mCurrentSector", c_int), ("mSpeedLimiter", c_ubyte),
    ("mMaxGears", c_ubyte), ("mFrontTireCompoundIndex", c_ubyte), ("mRearTireCompoundIndex", c_ubyte),
    ("mFuelCapacity", c_double), ("mFrontFlapActivated", c_ubyte), ("mRearFlapActivated", c_ubyte),
    ("mRearFlapLegalStatus", c_ubyte), ("mIgnitionStarter", c_ubyte), ("mFrontTireCompoundName", c_char * 18),
    ("mRearTireCompoundName", c_char * 18), ("mSpeedLimiterAvailable", c_ubyte), ("mAntiStallActivated", c_ubyte),
    ("mUnused", c_ubyte * 2), ("mVisualSteeringWheelRange", c_float), ("mRearBrakeBias", c_double),
    ("mTurboBoostPressure", c_double), ("mPhysicsToGraphicsOffset", c_float * 3),
    ("mPhysicalSteeringWheelRange", c_float), ("mDeltaBest", c_double), ("mBatteryChargeFraction", c_double),
    ("mElectricBoostMotorTorque", c_double), ("mElectricBoostMotorRPM", c_double),
    ("mElectricBoostMotorTemperature", c_double), ("mElectricBoostWaterTemperature", c_double),
    ("mElectricBoostMotorState", c_ubyte), ("mLapInvalidated", c_bool), ("mABSActive", c_bool),
    ("mTCActive", c_bool), ("mSpeedLimiterActive", c_bool), ("mWiperState", c_ubyte), ("mTC", c_ubyte),
    ("mTCMax", c_ubyte), ("mTCSlip", c_ubyte), ("mTCSlipMax", c_ubyte), ("mTCCut", c_ubyte), ("mTCCutMax", c_ubyte),
    ("mABS", c_ubyte), ("mABSMax", c_ubyte), ("mMotorMap", c_ubyte), ("mMotorMapMax", c_ubyte),
    ("mMigration", c_ubyte), ("mMigrationMax", c_ubyte), ("mFrontAntiSway", c_ubyte), ("mFrontAntiSwayMax", c_ubyte),
    ("mRearAntiSway", c_ubyte), ("mRearAntiSwayMax", c_ubyte), ("mLiftAndCoastProgress", c_ubyte),
    ("mTrackLimitsSteps", c_ubyte), ("mRegen", c_float), ("mStateOfCharge", c_float), ("mVirtualEnergy", c_float),
    ("mTimeGapCarAhead", c_float), ("mTimeGapCarBehind", c_float), ("mTimeGapPlaceAhead", c_float),
    ("mTimeGapPlaceBehind", c_float), ("mVehicleModel", c_char * 30), ("mVehicleClass", c_ubyte),
    ("mVehicleChampionship", c_ubyte), ("mExpansion", c_ubyte * 20), ("mWheels", Wheel * 4),
])

VehicleScoring = _struct("VehicleScoring", [
    ("mID", c_int), ("mDriverName", c_char * 32), ("mVehicleName", c_char * 64), ("mTotalLaps", c_short),
    ("mSector", c_byte), ("mFinishStatus", c_byte), ("mLapDist", c_double), ("mPathLateral", c_double),
    ("mTrackEdge", c_double), ("mBestSector1", c_double), ("mBestSector2", c_double), ("mBestLapTime", c_double),
    ("mLastSector1", c_double), ("mLastSector2", c_double), ("mLastLapTime", c_double), ("mCurSector1", c_double),
    ("mCurSector2", c_double), ("mNumPitstops", c_short), ("mNumPenalties", c_short), ("mIsPlayer", c_bool),
    ("mControl", c_byte), ("mInPits", c_bool), ("mPlace", c_ubyte), ("mVehicleClass", c_char * 32),
    ("mTimeBehindNext", c_double), ("mLapsBehindNext", c_int), ("mTimeBehindLeader", c_double),
    ("mLapsBehindLeader", c_int), ("mLapStartET", c_double), ("mPos", Vect3), ("mLocalVel", Vect3),
    ("mLocalAccel", Vect3), ("mOri", Vect3 * 3), ("mLocalRot", Vect3), ("mLocalRotAccel", Vect3),
    ("mHeadlights", c_ubyte), ("mPitState", c_ubyte), ("mServerScored", c_ubyte), ("mIndividualPhase", c_ubyte),
    ("mQualification", c_int), ("mTimeIntoLap", c_double), ("mEstimatedLapTime", c_double),
    ("mPitGroup", c_char * 24), ("mFlag", c_ubyte), ("mUnderYellow", c_bool), ("mCountLapFlag", c_ubyte),
    ("mInGarageStall", c_bool), ("mUpgradePack", c_ubyte * 16), ("mPitLapDist", c_float),
    ("mBestLapSector1", c_float), ("mBestLapSector2", c_float), ("mSteamID", c_ulonglong),
    ("mVehFilename", c_char * 32), ("mAttackMode", c_short), ("mFuelFraction", c_ubyte), ("mDRSState", c_bool),
    ("mExpansion", c_ubyte * 4),
])

ScoringInfo = _struct("ScoringInfo", [
    ("mTrackName", c_char * 64), ("mSession", c_int), ("mCurrentET", c_double), ("mEndET", c_double),
    ("mMaxLaps", c_int), ("mLapDist", c_double), ("mResultsStreamPointer", c_ubyte * 8), ("mNumVehicles", c_int),
    ("mGamePhase", c_ubyte), ("mYellowFlagState", c_char), ("mSectorFlag", c_ubyte * 3), ("mStartLight", c_ubyte),
    ("mNumRedLights", c_ubyte), ("mInRealtime", c_bool), ("mPlayerName", c_char * 32),
    ("mPlrFileName", c_char * 64), ("mDarkCloud", c_double), ("mRaining", c_double), ("mAmbientTemp", c_double),
    ("mTrackTemp", c_double), ("mWind", Vect3), ("mMinPathWetness", c_double), ("mMaxPathWetness", c_double),
    ("mGameMode", c_ubyte), ("mIsPasswordProtected", c_bool), ("mServerPort", c_ushort),
    ("mServerPublicIP", c_uint), ("mMaxPlayers", c_int), ("mServerName", c_char * 32), ("mStartET", c_float),
    ("mAvgPathWetness", c_double), ("mSessionTimeRemaining", c_float), ("mTimeOfDay", c_float),
    ("mIsFixedSetup", c_bool), ("mTrackGripLevel", c_ubyte), ("mCloudCoverage", c_ubyte),
    ("mTrackLimitsStepsPerPenalty", c_ubyte), ("mTrackLimitsStepsPerPoint", c_ubyte), ("mExpansion", c_ubyte * 187),
    ("mVehiclePointer", c_ubyte * 8),
])

ApplicationState = _struct("ApplicationState", [
    ("mAppWindow", c_ulonglong), ("mWidth", c_uint), ("mHeight", c_uint), ("mRefreshRate", c_uint),
    ("mWindowed", c_uint), ("mOptionsLocation", c_ubyte), ("mOptionsPage", c_char * 31), ("mExpansion", c_ubyte * 204),
])

Events = _struct("Events", [(name, c_uint) for name in (
    "SME_ENTER", "SME_EXIT", "SME_STARTUP", "SME_SHUTDOWN", "SME_LOAD", "SME_UNLOAD", "SME_START_SESSION",
    "SME_END_SESSION", "SME_ENTER_REALTIME", "SME_EXIT_REALTIME", "SME_UPDATE_SCORING", "SME_UPDATE_TELEMETRY",
    "SME_INIT_APPLICATION", "SME_UNINIT_APPLICATION", "SME_SET_ENVIRONMENT", "SME_FFB")])

Generic = _struct("Generic", [("events", Events), ("gameVersion", c_int), ("FFBTorque", c_float),
                              ("appInfo", ApplicationState)])
Paths = _struct("Paths", [(name, c_char * MAX_PATH) for name in (
    "userData", "customVariables", "stewardResults", "playerProfile", "pluginsFolder")])
ScoringData = _struct("ScoringData", [("scoringInfo", ScoringInfo), ("scoringStreamSize", c_ubyte * 12),
                                      ("vehScoringInfo", VehicleScoring * MAX_VEHICLES),
                                      ("scoringStream", c_char * 65536)])
TelemetryData = _struct("TelemetryData", [("activeVehicles", c_ubyte), ("playerVehicleIdx", c_ubyte),
                                          ("playerHasVehicle", c_bool),
                                          ("telemInfo", VehicleTelemetry * MAX_VEHICLES)])
SharedData = _struct("SharedData", [("generic", Generic), ("paths", Paths), ("scoring", ScoringData),
                                    ("telemetry", TelemetryData)])

# mSession : 0 = journée test, 1-4 = essais, 5-8 = qualifications, 9 = warmup, 10-13 = course
def session_type(session):
    if session >= 10:
        return "Race"
    if session == 9:
        return "Warmup"
    if session >= 5:
        return "Qualify"
    if session >= 1:
        return "Practice"
    return "Test day"


def _text(raw):
    return raw.decode("utf-8", "replace").strip() if raw else ""


# --- accès Windows à la mémoire partagée -------------------------------------------------------------

def _game_running():
    """Vrai si le processus du jeu tourne (liste des processus Windows, sans dépendance)."""
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return False
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == PROCESS_NAME.lower():
                return True
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return False
    finally:
        kernel32.CloseHandle(snapshot)


def open_shared_memory():
    """Ouvre la mémoire partagée du jeu, ou None s'il n'est pas lancé (hors Windows : toujours None)."""
    import mmap
    try:
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
    except AttributeError:  # pas Windows
        return None
    kernel32.OpenFileMappingW.restype = wintypes.HANDLE
    handle = kernel32.OpenFileMappingW(0x0004, False, SHARED_MEMORY_NAME)  # FILE_MAP_READ
    if not handle:
        return None  # jeu non lancé : surtout ne pas créer la zone nous-mêmes
    kernel32.CloseHandle(handle)
    try:
        return mmap.mmap(-1, ctypes.sizeof(SharedData), SHARED_MEMORY_NAME)  # lecture seule en pratique
    except OSError as exc:  # zone plus petite que prévu : version du jeu incompatible
        print(f"[LMU] mémoire partagée illisible ({exc!r}) : mise à jour de l'application nécessaire ?")
        return None


class LMUSDK:
    """Même interface que pyirsdk.IRSDK, alimentée par la mémoire partagée de Le Mans Ultimate."""

    name = "Le Mans Ultimate"

    def __init__(self, opener=open_shared_memory, is_running=_game_running):
        self.opener = opener
        self.is_running = is_running
        self.buffer = None
        self.game_version = 0
        self.is_initialized = False
        self._checked_at = 0.0
        self.values = {}
        self._sessions = {}  # (type de session, circuit, voiture, n° de relance) -> SessionNum
        self._session_key = None
        self._restarts = 0
        self._last_et = None
        self._lap_number = None
        self._lap_invalid = False
        self._last_lap_invalid = False
        self._last_pos = None
        self._yaw = 0.0

    # --- connexion (comme pyirsdk) --------------------------------------------------

    @property
    def is_connected(self):
        if self.buffer is None:
            return False
        now = time.monotonic()
        if now - self._checked_at > PROCESS_CHECK:
            self._checked_at = now
            if not self.is_running():
                return False
        return self.game_version != 0

    def startup(self):
        if self.buffer is None:
            self.buffer = self.opener()
            if self.buffer is None:
                return False
            self._checked_at = time.monotonic()
        self.is_initialized = True
        self.freeze_var_buffer_latest()
        return True

    def shutdown(self):
        self.is_initialized = False
        self.game_version = 0
        if self.buffer is not None:
            try:
                self.buffer.close()
            except (BufferError, AttributeError):
                pass
        self.buffer = None
        self.values = {}

    # --- lecture ----------------------------------------------------------------------

    def _read(self, struct, offset):
        return struct.from_buffer_copy(self.buffer[offset:offset + ctypes.sizeof(struct)])

    def freeze_var_buffer_latest(self):
        """Copie l'instant présent (seulement ta voiture) et le traduit en variables iRacing."""
        if self.buffer is None:
            return
        self.game_version = self._read(Generic, SharedData.generic.offset).gameVersion
        scoring_base = SharedData.scoring.offset
        info = self._read(ScoringInfo, scoring_base + ScoringData.scoringInfo.offset)
        telemetry_base = SharedData.telemetry.offset
        head = self.buffer[telemetry_base:telemetry_base + 3]
        player_idx, has_vehicle = head[1], bool(head[2])

        vehicle = None
        size = ctypes.sizeof(VehicleScoring)
        first = scoring_base + ScoringData.vehScoringInfo.offset
        flag = VehicleScoring.mIsPlayer.offset
        for i in range(max(0, min(info.mNumVehicles, MAX_VEHICLES))):
            if self.buffer[first + i * size + flag]:
                vehicle = self._read(VehicleScoring, first + i * size)
                break
        telem = None
        if has_vehicle and player_idx < MAX_VEHICLES:
            offset = telemetry_base + TelemetryData.telemInfo.offset + player_idx * ctypes.sizeof(VehicleTelemetry)
            telem = self._read(VehicleTelemetry, offset)
        self.values = self._translate(info, vehicle, telem)

    def unfreeze_var_buffer_latest(self):
        pass

    def __getitem__(self, key):
        return self.values.get(key)

    # --- traduction LMU -> variables iRacing -----------------------------------------

    def _session_num(self, info, car, track):
        if not track or not car:
            return None
        et = info.mCurrentET
        if self._last_et is not None and et < self._last_et - 5.0:
            self._restarts += 1  # session relancée : le chrono repart de zéro
        self._last_et = et
        key = (info.mSession, track, car, self._restarts)
        return self._sessions.setdefault(key, len(self._sessions))

    def _translate(self, info, vehicle, telem):
        track = _text(info.mTrackName)
        if telem is None or vehicle is None:
            return {"SessionNum": None, "IsOnTrack": False, "SessionTick": None}
        car = _text(telem.mVehicleModel) or _text(telem.mVehicleName)
        track_length = info.mLapDist if info.mLapDist > 0 else None
        et = telem.mElapsedTime
        vel, acc = telem.mLocalVel, telem.mLocalAccel
        speed = math.sqrt(vel.x ** 2 + vel.y ** 2 + vel.z ** 2)

        # Distance parcourue : le classement (5 fois par seconde) + ce qu'on a roulé depuis.
        lap_dist = vehicle.mLapDist
        if track_length:
            lap_dist += speed * min(max(et - info.mCurrentET, 0.0), 0.5)
            lap_dist_pct = (lap_dist % track_length) / track_length
        else:
            lap_dist_pct = 0.0

        # Cap (0 = nord, sens horaire) d'après le déplacement de la voiture sur le plan x/z du jeu.
        pos = telem.mPos
        if self._last_pos is not None:
            dx, dz = pos.x - self._last_pos[0], pos.z - self._last_pos[1]
            if dx * dx + dz * dz > 0.0025:  # 5 cm : en dessous, le cap serait du bruit
                self._yaw = math.atan2(dx, dz)
                self._last_pos = (pos.x, pos.z)
        else:
            self._last_pos = (pos.x, pos.z)

        # Tour hors limites de piste : LMU l'invalide, on le traite comme un tour sans temps.
        if telem.mLapNumber != self._lap_number:
            if self._lap_number is not None:
                self._last_lap_invalid = self._lap_invalid
            self._lap_number = telem.mLapNumber
            self._lap_invalid = False
        self._lap_invalid = self._lap_invalid or bool(telem.mLapInvalidated)
        last_lap = vehicle.mLastLapTime if vehicle.mLastLapTime > 0 and not self._last_lap_invalid else -1.0

        wheel_range = telem.mPhysicalSteeringWheelRange or telem.mVisualSteeringWheelRange or DEFAULT_WHEEL_RANGE
        session_num = self._session_num(info, car, track)
        return {
            "SessionTick": et,
            "SessionNum": session_num,
            "SessionTime": et,
            "IsOnTrack": bool(info.mInRealtime) and not vehicle.mInGarageStall,
            "OnPitRoad": bool(vehicle.mInPits),
            "Lap": telem.mLapNumber,
            "LapCompleted": telem.mLapNumber,  # seules les variations comptent
            "LapDistPct": lap_dist_pct,
            "LapLastLapTime": last_lap,
            "LapCurrentLapTime": et - telem.mLapStartET,
            "Speed": speed,
            # entrées réelles du pilote (avant aides), 0 à 1
            "Throttle": telem.mUnfilteredThrottle,
            "Brake": telem.mUnfilteredBrake,
            "Clutch": 1.0 - telem.mUnfilteredClutch,  # iRacing : 1 = pédale relâchée
            "Gear": telem.mGear,
            # volant : -1 (gauche) à 1 (droite) -> angle en radians, positif à gauche comme iRacing
            "SteeringWheelAngle": -telem.mUnfilteredSteering * math.radians(wheel_range) / 2,
            "YawNorth": self._yaw,
            "FuelLevel": telem.mFuel,
            "RPM": telem.mEngineRPM,
            # repère voiture LMU : x vers la gauche, z vers l'arrière
            "LatAccel": acc.x,
            "LongAccel": -acc.z,
            "BrakeABSactive": bool(telem.mABSActive),
            "WeekendInfo": {"TrackDisplayName": track,
                            "TrackLength": f"{track_length / 1000:.3f} km" if track_length else None},
            "DriverInfo": {"DriverCarIdx": 0, "DriverCarSLShiftRPM": None,
                           "DriverCarRedLine": telem.mEngineMaxRPM or None,
                           "Drivers": [{"CarIdx": 0, "CarScreenName": car}]},
            "SessionInfo": {"Sessions": [{"SessionNum": session_num, "SessionType": session_type(info.mSession)}]},
        }


class LMUWaiter:
    """LMU ne signale pas ses mises à jour : on relit souvent, les doublons sont écartés (SessionTick)."""

    def __init__(self):
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)  # sommeil précis à la milliseconde sous Windows
        except (AttributeError, OSError):
            pass

    def wait(self):
        time.sleep(TICK)

    def reset(self):
        pass
