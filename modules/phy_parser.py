"""
phy_parser.py — Step 4: Parse profile CSV/TXT files to .phy format.

Adapted from:
  NOAA_pipeline/PHY_broken_pipeline_code/NOAA_phy_broken_profiles.py
  NOAA_pipeline/PHY_broken_pipeline_code/NOAA_phy_functions.py

Key changes vs. originals:
  - Removed import of nc_datagraphs (not needed — nc generation is handled separately).
  - Removed module-level float_dict_info mutation: a fresh copy is made per profile to prevent
    values from leaking across profiles.
  - RBR_response_timeout_err now initialized to False before each profile loop (original had
    undefined reference if system_log.txt was processed after science_log.csv).
  - missionStartTimestamp initialized to empty string as fallback.
  - Wrapped per-profile processing in try/except; errors are logged and processing continues.
  - Exposes run() function returning structured result dict.

Original logic preserved:
  - Full getMETAInfo, getTXTInfo, getVITALInfo, getSCIInfo helper functions (verbatim).
  - Broken float detection in getSCIInfo (F10051/F10052/F9185 special handling).
  - All .phy file formatting: float_dict_info key-value block, drift block, GPS section,
    profile data array (6 columns: PRES, TEMP, PSAL, CNDC, TEMP_CNDC, NUM_VALUES_AVG).
  - File sort order: .meta → .system_log.txt → .vitals_log.csv → .science_log.csv
"""

import copy
import csv
import glob
import os
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Optional

from modules.logger import FloatLogger


# ============================================================================
# Template for float_dict_info (reset per profile to avoid cross-profile leakage)
# ============================================================================

_TEMPVAR = 'noval'
_TEMPVAR_DT = "99999999999999"
_TEMPVAR_STAT = "9"

_FLOAT_DICT_TEMPLATE = {
    "INTERNAL ID NUMBER": _TEMPVAR,
    "WMO ID NUMBER": _TEMPVAR,
    "TRANSMISSION ID NUMBER": _TEMPVAR,
    "PROFILE NUMBER": _TEMPVAR,
    "INSTRUMENT TYPE": _TEMPVAR,
    "WMO INSTRUMENT TYPE (TABLE 1770)": _TEMPVAR,
    "WMO RECORDER TYPE (TABLE 4770)": _TEMPVAR,
    "POSITIONING SYSTEM": "GPS",
    "PI": _TEMPVAR,
    "CONFIG ASCENT TIME OUT (MINUTES)": _TEMPVAR,
    "CONFIG ASCENT BUOYANCY NUDGE (COUNTS)": _TEMPVAR,
    "CONFIG COMP HYPER RETRACTION (COUNTS)": _TEMPVAR,
    "CONFIG PRES START CONTINUOUS PROF(DBAR)": _TEMPVAR,
    "CONFIG DEEP PROF DESCENT TIME (MINUTES)": _TEMPVAR,
    "CONFIG DEEP PROF PISTON POS (COUNTS)": _TEMPVAR,
    "CONFIG DEEP PROFILE PRESSURE (DBAR)": _TEMPVAR,
    "CONFIG DOWN TIME (MINUTES)": _TEMPVAR,
    "CONFIG CONTROLLER BOARD SERIAL NUMBER": _TEMPVAR,
    "CONFIG PISTON FULL EXTENSION (COUNTS)": _TEMPVAR,
    "CONFIG PISTON FULL RETRACTION (COUNTS)": _TEMPVAR,
    "CONFIG INITIAL BUOYANCY NUDGE (COUNTS)": _TEMPVAR,
    "CONFIG MAX PRESSURE BLADDER (COUNTS)": _TEMPVAR,
    "CONFIG MISSION PRELUDE TIME (MINUTES)": _TEMPVAR,
    "NUMBER OK LAUNCH VACUUM (COUNTS)": _TEMPVAR,
    "CONFIG PRES ACT PISTON POSITION(COUNTS)": _TEMPVAR,
    "CONFIG PARK DESCENT PERIOD (MINUTES)": _TEMPVAR,
    "CONFIG PARK PISTON POSITION (COUNTS)": _TEMPVAR,
    "CONFIG DRIFT PRESSURE (DBAR)": _TEMPVAR,
    "CONFIG PARKNPROF CYCLE COUNTER(COUNTS)": _TEMPVAR,
    "CONFIG SAMPLING SCHEME (DBAR)": _TEMPVAR,
    "CONFIG TELEMETRY RETRY INTERV(MINUTES)": _TEMPVAR,
    "TELEMETRY TIME OUT MIN (SECONDS)": _TEMPVAR,
    "CONFIG CLOCK PRESET START CYCLE FLAG": _TEMPVAR,
    "CONFIG UP TIME (MINUTES)": _TEMPVAR,
    "CONFIG DEBUGGING VERBOSITY": _TEMPVAR,
    "CONFIG DEBUG BIT MASK": _TEMPVAR,
    "CONFIG ASCENT SPEED (MBAR/S)": _TEMPVAR,
    "BIT MASK WHEN ICE DETECTION ACTIVE": _TEMPVAR,
    "TRANSMISSION REPETITION RATE (SEC)": _TEMPVAR,
    "CRITICAL TEMP UNDER ICE (DEG C)": _TEMPVAR,
    "ICE DETECTION PRESSURE (MAX) (DBAR)": _TEMPVAR,
    "ICE EVASION PRESSURE (MIN) (DBAR)": _TEMPVAR,
    "AIR BLADDER PRESSURE": _TEMPVAR,
    "AIR BLADDER PRESSURE (COUNTS)": _TEMPVAR,
    "CURRENT BLADDER PRESSURE (COUNTS)": _TEMPVAR,
    "CURRENT BLADDER PRESSURE (DBAR)": _TEMPVAR,
    "CPU BATTERY VOLTAGE (VOLTS)": _TEMPVAR,
    "CPU BATTERY VOLTAGE (VOLTS) AT SURFACE": _TEMPVAR,
    "DRIFT BATTERY VOLTAGE (VOLTS)": _TEMPVAR,
    "PUMP BATTERY VOLTAGE (VOLTS) AT SURFACE": _TEMPVAR,
    "SURFACE BATTERY VOLTAGE (VOLTS)": _TEMPVAR,
    "VACUUM AT END OF TRANSMISSION (IN HG)": _TEMPVAR,
    "VACUUM BEFORE AIR BLADDER FILL (IN HG)": _TEMPVAR,
    "VACUUM AFTER AIR BLADDER FILL (IN HG)": _TEMPVAR,
    "BATTERY CURRENT AIR PUMP ON (M AMPERE)": _TEMPVAR,
    "DRIFT BATTERY CURRENT (M AMPERE)": _TEMPVAR,
    "BATTERY CURRENT NOLOAD (M AMPERE)": _TEMPVAR,
    "ACTIVE BALLAST ADJUSTMENTS": _TEMPVAR,
    "BUOYANCY PUMP ON TIME": _TEMPVAR,
    "BOTTOM PISTON POSITION": _TEMPVAR,
    "GPS FIX TIME (SECONDS)": _TEMPVAR,
    "DRIFT PISTON POSITION": _TEMPVAR,
    "DRIFT PRESSURE PRE PROFILE (DBAR)": _TEMPVAR,
    "DRIFT TEMPERATURE PRE PROFILE (DEG C)": _TEMPVAR,
    "DRIFT SALINITY PRE PROFILE (PSU)": _TEMPVAR,
    "SURFACE PISTON POSITION": _TEMPVAR,
    "SURFACE PRESSURE (DBAR)": _TEMPVAR,
    "TIME DESCENT START (YYYYMMDDHHMMSS)": _TEMPVAR_DT,
    "TIME DESCENT START STATUS": _TEMPVAR_STAT,
    "TIME DESCENT END (YYYYMMDDHHMMSS)": _TEMPVAR_DT,
    "TIME DESCENT END STATUS": _TEMPVAR_STAT,
    "TIME DRIFT START (YYYYMMDDHHMMSS)": _TEMPVAR_DT,
    "TIME DRIFT START STATUS": _TEMPVAR_STAT,
    "TIME DRIFT END (YYYYMMDDHHMMSS)": _TEMPVAR_DT,
    "TIME DRIFT END STATUS": _TEMPVAR_STAT,
    "TIME ASCENT START (YYYYMMDDHHMMSS)": _TEMPVAR_DT,
    "TIME ASCENT START STATUS": _TEMPVAR_STAT,
    "TIME ASCENT END (YYYYMMDDHHMMSS)": _TEMPVAR_DT,
    "TIME ASCENT END STATUS": _TEMPVAR_STAT,
    "START OF TRANSMISSION (YYYYMMDDHHMMSS)": _TEMPVAR_DT,
    "START OF TRANSMISSION STATUS": 2,
    "INTERNAL VACUUM (IN HG)": _TEMPVAR,
    "PROFILES OVERLAP": "false",
    "NUMBER OF COLUMNS": 6,
    "1.  COLUMN": "PRESSURE (DBAR)",
    "2.  COLUMN": "TEMPERATURE-90 (DEGC)",
    "3.  COLUMN": "SALINITY (PSU)",
    "4.  COLUMN": "CONDUCTIVITY (MS/CM)",
    "5.  COLUMN": "TEMP_CNDC (DEGC)",
    "6.  COLUMN": "NUMBER OF VALUES AVERAGED",
}


# ============================================================================
# Helper functions (adapted verbatim from NOAA_phy_functions.py)
# ============================================================================

def _getMETAInfo(meta_file: str, float_dict_info: dict):
    with open(meta_file, "r") as m_file:
        for line in m_file:
            line = line.strip()
            if 'internal ID number' in line:
                float_dict_info.update({"INTERNAL ID NUMBER": line.split()[-1]})
            if 'WMO ID number' in line:
                float_dict_info.update({"WMO ID NUMBER": line.split()[-1]})
            if 'transmission ID number' in line:
                float_dict_info.update({"TRANSMISSION ID NUMBER": line.split()[-1]})
            if 'instrument type' in line:
                float_dict_info.update({"INSTRUMENT TYPE": line.split()[-1]})
            if 'WMO instrument type (table 1770)' in line:
                float_dict_info.update({"WMO INSTRUMENT TYPE (TABLE 1770)": line.split()[-1]})
            if 'WMO recorder type (table 4770)' in line:
                float_dict_info.update({"WMO RECORDER TYPE (TABLE 4770)": line.split()[-1]})
            if 'Board serial number' in line:
                float_dict_info.update({"CONFIG CONTROLLER BOARD SERIAL NUMBER": line.split()[-1]})
            if 'MAX PRESSURE BLADDER (COUNTS)' in line:
                float_dict_info.update({"CONFIG MAX PRESSURE BLADDER (COUNTS)": line.split()[-1]})
            if 'PISTON FULL EXTENSION (COUNTS)' in line:
                float_dict_info.update({"CONFIG PISTON FULL EXTENSION (COUNTS)": line.split()[-1]})
            if 'PISTON FULL RETRACTION (COUNTS)' in line:
                float_dict_info.update({"CONFIG PISTON FULL RETRACTION (COUNTS)": line.split()[-1]})
            if 'NUMBER OK LAUNCH VACUUM (dbar)' in line:
                result = float(line.split()[-1]) * 2.953
                float_dict_info.update({"NUMBER OK LAUNCH VACUUM (COUNTS)": round(result, 4)})
            if 'PI' == line.split()[0]:
                float_dict_info.update({"PI": " ".join(line.split()[1:])})

def _getTXTInfo(txt_file: str, float_dict_info: dict, buoyancy_pump_on_time: float):
    prev_line = []
    config_sample_scheme = []
    foundBallastAdjustmentInfo = False
    buoyancy_pump_on_time = 0
    deepDescentToAscent = False
    foundSurfaceCompleteMissionLine = False
    storeLastBuoyEngine = []
    foundMissionStatePark = False
    foundMissionStateSurface = False
    foundDriftPistonPos = False
    RBR_response_timeout_err = False
    missionStartTimestamp = ""

    with open(txt_file, encoding='utf8', errors="ignore") as sys_file:
        for line in sys_file:
            line = line.strip()
            if 'AscentTimeout' in line:
                float_dict_info.update({"CONFIG ASCENT TIME OUT (MINUTES)": line.split()[-1]})
            if '|BuoyancyNudge' in line:
                float_dict_info.update({"CONFIG ASCENT BUOYANCY NUDGE (COUNTS)": line.split()[-1]})
            if 'HyperRetractCount' in line:
                float_dict_info.update({"CONFIG COMP HYPER RETRACTION (COUNTS)": line.split()[-1]})
            if 'DeepDescentPressure' in line:
                float_dict_info.update({"CONFIG DEEP PROFILE PRESSURE (DBAR)": line.split()[-1]})
            if 'DownTime' in line:
                float_dict_info.update({"CONFIG DOWN TIME (MINUTES)": line.split()[-1]})
            if 'InitialBuoyancyNudge' in line:
                float_dict_info.update({"CONFIG INITIAL BUOYANCY NUDGE (COUNTS)": line.split()[-1]})
            if 'PreludeTime' in line:
                float_dict_info.update({"CONFIG MISSION PRELUDE TIME (MINUTES)": line.split()[-1]})
            if 'ParkPressure' in line:
                float_dict_info.update({"CONFIG DRIFT PRESSURE (DBAR)": line.split()[-1]})
            if 'TelemetryTimeout' in line:
                float_dict_info.update({"TELEMETRY TIME OUT MIN (SECONDS)": int(line.split()[-1]) * 60})
            if 'UpTime' in line:
                float_dict_info.update({"CONFIG UP TIME (MINUTES)": int(line.split()[-1])})
            if 'LogVerbosity' in line:
                float_dict_info.update({"CONFIG DEBUGGING VERBOSITY": line.split()[-1]})
            if ('<PARK>' in prev_line) and ('|sample_cfg|SAMPLE PTSCI' in line):
                float_dict_info.update({"CONFIG PRES START CONTINUOUS PROF(DBAR)": line.split()[2]})
            if '|mission_cfg|DeepDescentTimeout' in line:
                float_dict_info.update({"CONFIG DEEP PROF DESCENT TIME (MINUTES)": line.split()[-1]})
            if '|mission_cfg|DeepDescentCount' in line:
                float_dict_info.update({"CONFIG DEEP PROF PISTON POS (COUNTS)": line.split()[-1]})
            if 'MActivationCount' in line:
                float_dict_info.update({"CONFIG PRES ACT PISTON POSITION(COUNTS)": line.split()[-1]})
            if 'ParkDescentTimeout' in line:
                float_dict_info.update({"CONFIG PARK DESCENT PERIOD (MINUTES)": line.split()[-1]})
            if 'mission_cfg|ParkDescentCount' in line:
                float_dict_info.update({"CONFIG PARK PISTON POSITION (COUNTS)": line.split()[-1]})
            if 'Starting Mission No.:' in line:
                missionStartTimestamp = line.split()[0]
                float_dict_info.update({"CONFIG PARKNPROF CYCLE COUNTER(COUNTS)": line.split()[-1]})
            if '<ASCENT>' in prev_line:
                config_sample_scheme = line.split()
            if ('PROFILE LGR' in line) and (len(config_sample_scheme) > 0):
                avg_window = line.split()[4]
                result = "{};{};{};{},{}".format(avg_window, avg_window, avg_window,
                                                 config_sample_scheme[3], config_sample_scheme[2])
                float_dict_info.update({"CONFIG SAMPLING SCHEME (DBAR)": result})
            if 'TelemetryInterval' in line:
                float_dict_info.update({"CONFIG TELEMETRY RETRY INTERV(MINUTES)": str(round(int(line.split()[-1]) / 60))})
                float_dict_info.update({"TRANSMISSION REPETITION RATE (SEC)": line.split()[-1]})
            if 'VitalsMask' in line:
                float_dict_info.update({"CONFIG DEBUG BIT MASK": "0x{}".format(line.split()[-1])})
            if 'PARKDESCENT -> PARK' in line:
                foundMissionStatePark = True
                foundBallastAdjustmentInfo = True
                countBallastAdjustment = 0
            if ('PARK -> DEEPDESCENT' in line) or ('PARK -> ASCENT' in line):
                foundBallastAdjustmentInfo = False
                float_dict_info.update({"ACTIVE BALLAST ADJUSTMENTS": countBallastAdjustment})
            if foundBallastAdjustmentInfo:
                if 'BuoyEngine|adjusting from' in line:
                    countBallastAdjustment += 1
            if (len(storeLastBuoyEngine) == 0) or (not foundDriftPistonPos):
                if ('BuoyEngine' in line) and (not foundMissionStatePark):
                    storeLastBuoyEngine = line
            if ('BuoyEngine' in line) and ('BuoyEngine' in prev_line) and \
               ('Requested buoyancy position' not in prev_line) and \
               ('Within 2 counts, not moving' not in line):
                num1 = int(prev_line.split()[2])
                num2 = int(prev_line.split()[-1])
                if num1 < num2:
                    temp_num = line.split()[-1]
                    mins = int(temp_num.split(':')[0]) * 60
                    secs = int(temp_num.split(':')[1])
                    buoyancy_pump_on_time += mins + secs
                if not foundMissionStatePark:
                    storeLastBuoyEngine = line
                    foundDriftPistonPos = True
                if foundMissionStateSurface:
                    float_dict_info.update({"SURFACE PISTON POSITION": line.split()[1]})
                    foundMissionStateSurface = False
            if 'DEEPDESCENT -> ASCENT' in line:
                deepDescentToAscent = True
            if deepDescentToAscent:
                if 'BuoyEngine' in line:
                    float_dict_info.update({"BOTTOM PISTON POSITION": line.split()[2]})
                    deepDescentToAscent = False
            if 'SURFACE|Completing Mission No.:' in line:
                foundSurfaceCompleteMissionLine = True
            if foundSurfaceCompleteMissionLine:
                if 'GPS TimeToFix:' in line:
                    float_dict_info.update({"GPS FIX TIME (SECONDS)": line.split()[2]})
            if 'ASCENT -> SURFACE' in line:
                foundMissionStateSurface = True
            if 'PARKDESCENT|surface pressure offset updated from' in line:
                float_dict_info.update({"SURFACE PRESSURE (DBAR)": line.split()[7]})
            if 'AscentStartTimes' in line:
                nums = [int(line.split()[i]) for i in range(1, 5)]
                if any(n == -1 for n in nums):
                    result = 0
                else:
                    result = ' '.join(str(n) for n in nums)
                float_dict_info.update({"CONFIG CLOCK PRESET START CYCLE FLAG": result})
            if 'AscentRate' in line:
                float_dict_info.update({"CONFIG ASCENT SPEED (MBAR/S)": line.split()[-1]})
            if 'IceMonths' in line:
                float_dict_info.update({"BIT MASK WHEN ICE DETECTION ACTIVE": f'0x{line.split()[-1]}'})
            if 'IceCriticalT' in line:
                float_dict_info.update({"CRITICAL TEMP UNDER ICE (DEG C)": line.split()[-1]})
            if 'IceDetectionP' in line:
                float_dict_info.update({"ICE DETECTION PRESSURE (MAX) (DBAR)": line.split()[-1]})
            if 'IceEvasionP' in line:
                float_dict_info.update({"ICE EVASION PRESSURE (MIN) (DBAR)": line.split()[-1]})
            if '|RBR|fetch|Response Timeout, Error:6' in line:
                RBR_response_timeout_err = True
            prev_line = line

    # End-of-file: update START OF TRANSMISSION + DRIFT PISTON POSITION
    if prev_line:
        result = prev_line.split('|')[0].replace('T', '')
        float_dict_info.update({"START OF TRANSMISSION (YYYYMMDDHHMMSS)": result})
    if len(storeLastBuoyEngine) > 0:
        float_dict_info.update({"DRIFT PISTON POSITION": storeLastBuoyEngine.split()[1]})

    return buoyancy_pump_on_time, missionStartTimestamp, RBR_response_timeout_err


def _getVITALInfo(csv_file, float_dict: dict):
    reader = csv.reader(csv_file)
    counter = 0
    for row in reader:
        if row[0] == "VITALS_CORE":
            counter += 1
            if counter == 1:
                float_dict.update({"AIR BLADDER PRESSURE": row[2]})
                float_dict.update({"AIR BLADDER PRESSURE (COUNTS)": row[3]})
                float_dict.update({"CPU BATTERY VOLTAGE (VOLTS)": row[4]})
                float_dict.update({"BATTERY CURRENT AIR PUMP ON (M AMPERE)": row[11]})
            elif counter == 2:
                float_dict.update({"CPU BATTERY VOLTAGE (VOLTS) AT SURFACE": row[4]})
                float_dict.update({"VACUUM AT END OF TRANSMISSION (IN HG)": round(float(row[8]) * 2.953, 2)})
            elif counter == 3:
                float_dict.update({"DRIFT BATTERY VOLTAGE (VOLTS)": row[4]})
                float_dict.update({"DRIFT BATTERY CURRENT (M AMPERE)": row[11]})
            elif counter == 6:
                float_dict.update({"PUMP BATTERY VOLTAGE (VOLTS) AT SURFACE": row[4]})
                float_dict.update({"VACUUM BEFORE AIR BLADDER FILL (IN HG)": round(float(row[8]) * 2.953, 2)})
                float_dict.update({"BATTERY CURRENT NOLOAD (M AMPERE)": row[11]})
            elif counter == 7:
                float_dict.update({"INTERNAL VACUUM (IN HG)": round(float(row[8]) * 2.953, 4)})
                float_dict.update({"CURRENT BLADDER PRESSURE (DBAR)": row[2]})
                float_dict.update({"CURRENT BLADDER PRESSURE (COUNTS)": row[3]})
                float_dict.update({"SURFACE BATTERY VOLTAGE (VOLTS)": row[4]})
                float_dict.update({"VACUUM AFTER AIR BLADDER FILL (IN HG)": round(float(row[8]) * 2.953, 2)})


def _getSCIInfo(csv_file, float_dict: dict, RBR_response_timeout_err: bool):
    PTSCIinfo = []
    reader = csv.reader(csv_file)
    prev_row = []
    last_PTSCI_val = []
    foundASCENTflag = False
    GPS = []
    driftInfo = []
    foundParkDescentMission = False
    BROKEN_DRIFT_FLAG = False
    BROKEN_CP_PTSCI_ASCENT_FLAG = False
    BROKEN_ptsci = []
    BROKEN_drift = []
    CP_started_flag = False
    valid_cp_flag = True

    for row in reader:
        if row[2] == "Park Descent Mission":
            float_dict.update({"TIME DESCENT START (YYYYMMDDHHMMSS)": row[1].replace('T', '')})
            float_dict.update({"TIME DESCENT START STATUS": '2'})
            foundParkDescentMission = True
        if row[2] == "Park Mission":
            BROKEN_DRIFT_FLAG = True
            float_dict.update({"TIME DRIFT START (YYYYMMDDHHMMSS)": row[1].replace('T', '')})
            float_dict.update({"TIME DRIFT START STATUS": '2'})
            float_dict.update({"TIME DESCENT END (YYYYMMDDHHMMSS)": prev_row[1].replace('T', '')})
            float_dict.update({"TIME DESCENT END STATUS": '2'})
        if row[2] == "ASCENT":
            foundASCENTflag = True
            BROKEN_DRIFT_FLAG = False
            BROKEN_CP_PTSCI_ASCENT_FLAG = True
            float_dict.update({"TIME ASCENT START (YYYYMMDDHHMMSS)": row[1].replace('T', '')})
            float_dict.update({"TIME ASCENT START STATUS": '2'})
            float_dict.update({"TIME DRIFT END (YYYYMMDDHHMMSS)": prev_row[1].replace('T', '')})
            float_dict.update({"TIME DRIFT END STATUS": '2'})
        if row[2] == "Surface Mission":
            float_dict.update({"TIME ASCENT END (YYYYMMDDHHMMSS)": prev_row[1].replace('T', '')})
            float_dict.update({"TIME ASCENT END STATUS": '2'})
        if row[2] == "CP started":
            CP_started_flag = True
        if row[2] in ("CP stopped", "CP already finished"):
            if not CP_started_flag:
                valid_cp_flag = False
        if BROKEN_CP_PTSCI_ASCENT_FLAG:
            if row[0] == 'LGR_PTSCI':
                r = list(row) + ["-99"]
                BROKEN_ptsci.append(r[2:])
            if row[0] == 'LGR_CP_PTSCI':
                BROKEN_ptsci.append(row)
        if BROKEN_DRIFT_FLAG:
            BROKEN_drift.append(row)
        if row[0] == "LGR_CP_PTSCI":
            PTSCIinfo.append(row)
        if row[0] == 'GPS':
            GPS.append(row)
        if row[2] in ("Deep Descent Mission", "ASCENT"):
            foundParkDescentMission = False
        if row[0] == "LGR_PTSCI":
            last_PTSCI_val = row
        if foundASCENTflag and last_PTSCI_val:
            float_dict.update({"DRIFT PRESSURE PRE PROFILE (DBAR)": last_PTSCI_val[2]})
            float_dict.update({"DRIFT TEMPERATURE PRE PROFILE (DEG C)": last_PTSCI_val[3]})
            float_dict.update({"DRIFT SALINITY PRE PROFILE (PSU)": last_PTSCI_val[4]})
            foundASCENTflag = False
        if foundParkDescentMission:
            driftInfo.append(row)
        prev_row = row

    if len(PTSCIinfo) <= 10:
        if float_dict["INTERNAL ID NUMBER"] in ["9542", "9543"]:
            if not valid_cp_flag:
                if RBR_response_timeout_err:
                    if len(BROKEN_ptsci) >= 20:
                        return BROKEN_ptsci, GPS, BROKEN_drift
                    else:
                        raise Exception("Failed check: length of BROKEN_ptsci data < 20")
                else:
                    raise Exception("Failed check: no RBR response timeout err in system log")
            else:
                raise Exception("Failed check: valid CP flags associated with data")
        elif float_dict["INTERNAL ID NUMBER"] in ["10408"]:
            return [], GPS, BROKEN_drift
        else:
            raise Exception("Float not in known broken float list")
    else:
        return PTSCIinfo, GPS, driftInfo


# ============================================================================
# File sort order (meta → system → vitals → science)
# ============================================================================

def _sort_key(filename: str) -> int:
    if filename.endswith('.meta'):
        return 0
    elif filename.endswith('.system_log.txt'):
        return 1
    elif filename.endswith('.vitals_log.csv'):
        return 2
    elif filename.endswith('.science_log.csv'):
        return 3
    return 4


# ============================================================================
# Per-profile .phy file generation (adapted from NOAA_files())
# ============================================================================

def _generate_phy_file(
    profile_tag: str,
    file_list: list,
    data_dir: str,
    phy_dir: str,
    phy_filename_prefix: str,
    meta_file: str,
) -> str:
    """
    Generate a single .phy file for one profile.
    Returns the path to the generated .phy file.
    Raises on any unrecoverable error (caller catches and logs).
    """
    profile_num = profile_tag[9:12]
    float_dict_info = copy.deepcopy(_FLOAT_DICT_TEMPLATE)
    float_dict_info["PROFILE NUMBER"] = profile_num

    date = "YEAR/MO/DT"
    timestamp = "HR:MN:SC"
    xmits = 'N/A'
    pos_qc = '0'
    buoyancy_pump_on_time = 0
    RBR_response_timeout_err = False  # initialized before loop (fix for original undefined reference)
    missionStartTimestamp = ""
    PTSCI_col_info = []
    GPS = []
    driftInfo = []

    sorted_files = sorted(file_list, key=_sort_key)

    for v in sorted_files:
        if 'meta' in v:
            _getMETAInfo(meta_file, float_dict_info)

        elif '.system_log.txt' in v:
            system_file = os.path.join(data_dir, v)
            buoyancy_pump_on_time, missionStartTimestamp, RBR_response_timeout_err = \
                _getTXTInfo(system_file, float_dict_info, buoyancy_pump_on_time)
            float_dict_info.update({"BUOYANCY PUMP ON TIME": buoyancy_pump_on_time})

        elif '.vitals_log.csv' in v:
            vital_file = os.path.join(data_dir, v)
            with open(vital_file, mode='r') as vit_file:
                _getVITALInfo(vit_file, float_dict_info)

        elif '.science_log.csv' in v:
            science_file = os.path.join(data_dir, v)
            with open(science_file, mode='r') as sci_file:
                PTSCI_col_info, GPS, driftInfo = _getSCIInfo(sci_file, float_dict_info, RBR_response_timeout_err)

    # Organize drift data into 4-value chunks
    driftPressure, driftTemperature, driftSalinity, driftTime = [], [], [], []
    for row in driftInfo:
        if row[0] == 'LGR_PTSCI':
            driftTime.append(row[1].replace('T', ''))
            driftPressure.append(row[2])
            driftTemperature.append(row[3])
            driftSalinity.append(row[4])
        if row[0] == 'LGR_PT':
            driftTime.append(row[1].replace('T', ''))
            driftPressure.append(row[2])
            driftTemperature.append(row[3])

    def chunker(seq, size):
        return (seq[pos:pos + size] for pos in range(0, len(seq), size))

    phy_path = os.path.join(phy_dir, f"{phy_filename_prefix}_{profile_num}.phy")
    with open(phy_path, 'w') as textfile:
        for k, v in float_dict_info.items():
            if k in ("CRITICAL TEMP UNDER ICE (DEG C)", "ICE DETECTION PRESSURE (MAX) (DBAR)",
                     "ICE EVASION PRESSURE (MIN) (DBAR)") and v == _TEMPVAR:
                continue
            textfile.write(f'{k:<40}{v}\n')
            if k == "PI":
                for pressure in chunker(driftPressure, 4):
                    textfile.write(f'{"DRIFT PRESSURE (DBAR)":<40}')
                    for element in pressure:
                        textfile.write(f'{element} ')
                    textfile.write('\n')
                for temp in chunker(driftTemperature, 4):
                    textfile.write(f'{"DRIFT TEMPERATURE (DEG C)":<40}')
                    for element in temp:
                        textfile.write(f'{element} ')
                    textfile.write('\n')
                for salinity in chunker(driftSalinity, 4):
                    textfile.write(f'{"DRIFT SALINITY (PSU)":<40}')
                    for element in salinity:
                        textfile.write(f'{element} ')
                    textfile.write('\n')
                for t in chunker(driftTime, 4):
                    textfile.write(f'{"DRIFT MEASUREMENT TIME (YYYYMMDDHHMMSS)":<40}')
                    for element in t:
                        textfile.write(f'{element} ')
                    textfile.write('\n')

        textfile.write('=' * 71 + '\n')

        if len(GPS) > 0:
            textfile.write('   LATITUDE  LONGITUDE YEAR/MO/DY HR:MN:SC XMITS SAT POSITION-QC CYC\n')
            try:
                mission_start_dt = None
                if missionStartTimestamp:
                    ms = missionStartTimestamp.split('|')[0]
                    ms_parts = ms.split('T')
                    if len(ms_parts) == 2:
                        sd = textwrap.wrap(ms_parts[0], 2)
                        st = textwrap.wrap(ms_parts[1], 2)
                        mission_start_dt = datetime.strptime(
                            f'{"".join(sd[:2])}/{sd[2]}/{sd[3]} {st[0]}:{st[1]}:{st[2]}',
                            "%Y/%m/%d %H:%M:%S"
                        )
            except Exception:
                mission_start_dt = None

            for row in GPS:
                lat = round(float(row[2]), 4)
                lon = round(float(row[3]), 4)
                lat_str = f"+{lat}" if lat > 0 else str(lat)
                lon_str = f"+{lon}" if lon > 0 else str(lon)
                sat_val = row[4]
                try:
                    gps_parts = row[1].split('T')
                    gd = textwrap.wrap(gps_parts[0], 2)
                    gt = textwrap.wrap(gps_parts[1], 2)
                    gps_date_str = f'{"".join(gd[:2])}/{gd[2]}/{gd[3]}'
                    gps_time_str = f'{gt[0]}:{gt[1]}:{gt[2]}'
                    gps_dt = datetime.strptime(f'{gps_date_str} {gps_time_str}', "%Y/%m/%d %H:%M:%S")
                    date = gps_date_str
                    timestamp = gps_time_str
                    cyc_num = int(profile_num) - 1 if (mission_start_dt and mission_start_dt > gps_dt) else int(profile_num)
                except Exception:
                    cyc_num = int(profile_num)
                textfile.write(f'   {lat_str:>8}{lon_str:>11}{date:>11}{timestamp:>9}{xmits:>6}{sat_val:>4}{pos_qc:>8}{cyc_num:>7}\n')
        else:
            textfile.write('   LATITUDE  LONGITUDE YEAR/MO/DY HR:MN:SC XMITS SAT POSITION-QC\n')
            textfile.write('    +99.999   +999.999 9999/99/99 99:99:99   000   0       0\n')

        textfile.write('=' * 71 + '\n')

        for row in PTSCI_col_info:
            columns = row[-6:]
            formatted_row = [str(val).ljust(12) for val in columns]
            textfile.write(' '.join(formatted_row) + '\n')

    return phy_path


# ============================================================================
# Public API
# ============================================================================

def run(
    profiles_dir: str,
    phy_dir: str,
    float_id: str,
    aoml_id: str,
    meta_file: Optional[str],
    logger: FloatLogger,
    force_reprocess: bool = False,
) -> dict:
    """
    Parse profile CSV/TXT files to .phy format.

    Args:
        profiles_dir:     PROFILES/ directory containing science_log.csv, vitals_log.csv, system_log.txt.
        phy_dir:          PHY_FILES/ output directory.
        float_id:         Float ID string (e.g. 'F10051'), used for logging.
        aoml_id:          AOML internal ID (e.g. '9542'), used in output filename prefix.
        meta_file:        Path to .meta file. If None or empty, phy parsing is skipped.
        logger:           FloatLogger instance.
        force_reprocess:  If True, regenerate .phy files even if they already exist.

    Returns:
        dict with keys:
            phy_files (list of str — paths of generated .phy files)
            errors    (list of str — profile tags that failed)
    """
    if not meta_file:
        logger.info("PHY_PARSING", "No meta_file configured — skipping .phy parsing.")
        logger.success("PHY_PARSING")
        return {"phy_files": [], "errors": []}

    if not os.path.exists(meta_file):
        logger.error("PHY_PARSING", f"meta_file not found: {meta_file} — skipping .phy parsing.")
        return {"phy_files": [], "errors": []}

    os.makedirs(phy_dir, exist_ok=True)

    # Group profile files by their 12-char prefix tag
    all_files = list(Path(profiles_dir).glob("*"))
    unique_tags: dict[str, list] = {}
    for f in all_files:
        if f.suffix not in ('.txt', '.csv'):
            continue
        tag = f.name[:12]
        unique_tags.setdefault(tag, [])
        unique_tags[tag].append(f.name)
        if meta_file not in unique_tags[tag]:
            unique_tags[tag].append(os.path.basename(meta_file))  # signal to process meta

    if not unique_tags:
        logger.info("PHY_PARSING", "No profile files found in PROFILES/ — nothing to parse.")
        logger.success("PHY_PARSING")
        return {"phy_files": [], "errors": []}

    logger.info("PHY_PARSING", f"Profiles to parse: {len(unique_tags)}")

    # phy filename prefix: {aoml_id}_{float_id padded to 6 digits}
    float_num = ''.join(filter(str.isdigit, float_id))
    phy_prefix = f"{aoml_id}_{float_num.zfill(6)}"

    phy_files = []
    errors = []

    for tag, file_list in unique_tags.items():
        profile_num = tag[9:12]
        phy_path = os.path.join(phy_dir, f"{phy_prefix}_{profile_num}.phy")
        if os.path.exists(phy_path) and not force_reprocess:
            logger.file_only("PHY_PARSING", f"Profile {profile_num}: already exists — skipping.")
            phy_files.append(phy_path)
            continue
        try:
            phy_path = _generate_phy_file(tag, file_list, profiles_dir, phy_dir, phy_prefix, meta_file)
            phy_files.append(phy_path)
            logger.info("PHY_PARSING", f"Profile {profile_num}: success → {os.path.basename(phy_path)}")
        except Exception as e:
            errors.append(profile_num)
            logger.error("PHY_PARSING", f"Profile {profile_num}: {e}")

    if not errors:
        logger.success("PHY_PARSING")

    return {"phy_files": phy_files, "errors": errors}
