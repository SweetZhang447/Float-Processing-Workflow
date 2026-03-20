"""
nc_from_csv.py — Step 5: Parse profile CSV/TXT files to intermediate netCDF format.

Adapted from make_origin_nc_files.py::read_csv_files() and make_nc_file_origin().

Key changes vs. original:
  - Exposes run() function returning structured result dict.
  - Integrates with FloatLogger for error logging.
  - Per-profile try/except: errors logged, processing continues to next profile.
  - sys.path.insert used to import to_julian_day and make_nc_file_origin from dmode_tools/tools.py.

Original logic preserved verbatim:
  - broken_float flag: 0 = LGR_CP_PTSCI only, 1 = LGR_PTSCI + LGR_CP_PTSCI during ASCENT.
  - All commented-out bin-averaging code kept as-is (scipy bin avg for broken F10051).
  - ARGO convention array reversal (increasing pressure order).
  - PRES_ADJUSTED = PRES - surface_pressure_offset from system_log.txt.
  - PTSCI_TIMESTAMPS array from science_log.csv row[1] (YYYYMMDDTHHMMSS).
  - Fallback JULD logic: CP started → Surface Mission → first PTSCI timestamp → np.nan.
  - conductivity scaling: cndc / 10.
  - make_nc_file_origin() called from dmode_tools; output: {float_num}-{profile_num:03}.nc.

NOTE: tools.py (and make_nc_file_origin from make_origin_nc_files.py) must be importable
from the path configured in DMODE_TOOLS_PATH below or in config.json under dmode_tools_path.
"""

import copy
import csv
import glob
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

from modules.logger import FloatLogger

# ============================================================================
# dmode_tools import — sys.path injection
# ============================================================================

# Default path to dmode_tools. Can be overridden at runtime via _configure_dmode_path().
_DMODE_TOOLS_PATH: Optional[str] = r"C:\Users\szswe\Desktop\DMODE_processing\dmode_tools"


def _configure_dmode_path(path: str):
    """Call from workflow.py with the dmode_tools directory path if it differs from default."""
    global _DMODE_TOOLS_PATH
    _DMODE_TOOLS_PATH = path


def _ensure_dmode_imported():
    """Inject dmode_tools into sys.path and return (to_julian_day, make_nc_file_origin)."""
    if _DMODE_TOOLS_PATH and _DMODE_TOOLS_PATH not in sys.path:
        sys.path.insert(0, _DMODE_TOOLS_PATH)
    try:
        from tools import to_julian_day  # noqa: F401
        from make_origin_nc_files import make_nc_file_origin  # noqa: F401
        return to_julian_day, make_nc_file_origin
    except ImportError as e:
        raise ImportError(
            f"Could not import from dmode_tools at '{_DMODE_TOOLS_PATH}': {e}\n"
            "Set 'dmode_tools_path' in config.json to the correct path."
        ) from e


# ============================================================================
# Public API
# ============================================================================

def run(
    profiles_dir: str,
    nc_dir: str,
    float_num: str,
    broken_float: int,
    logger: FloatLogger,
    force_reprocess: bool = False,
) -> dict:
    """
    Parse profile CSV/TXT files to intermediate netCDF format.

    Args:
        profiles_dir:     PROFILES/ directory containing science_log.csv + system_log.txt pairs.
        nc_dir:           Output directory for intermediate .nc files ({float_num}-{profile_num:03}.nc).
        float_num:        Float identifier (e.g. 'F9186').
        broken_float:     0 = normal (LGR_CP_PTSCI only), 1 = broken (LGR_PTSCI + LGR_CP_PTSCI during ascent).
        logger:           FloatLogger instance.
        force_reprocess:  If True, regenerate .nc files even if they already exist.

    Returns:
        dict with keys:
            nc_files (list of str — paths of generated .nc files)
            errors   (list of str — profile numbers that failed)
    """
    try:
        to_julian_day, make_nc_file_origin = _ensure_dmode_imported()
    except ImportError as e:
        logger.error("NC_FROM_CSV", str(e))
        return {"nc_files": [], "errors": []}

    os.makedirs(nc_dir, exist_ok=True)

    # Group files by profile number (chars 9-12 of filename)
    all_files = (
        p.resolve() for p in Path(profiles_dir).glob("*")
        if p.name.endswith("system_log.txt") or p.name.endswith("science_log.csv")
    )
    files_dict: dict[str, dict] = {}
    for fp in all_files:
        profile_num = fp.name[9:12]
        files_dict.setdefault(profile_num, {})
        if "science_log.csv" in fp.name:
            files_dict[profile_num]["science_log"] = fp
        elif "system_log.txt" in fp.name:
            files_dict[profile_num]["system_log"] = fp

    if not files_dict:
        logger.info("NC_FROM_CSV", "No profile pairs found in PROFILES/ — nothing to convert.")
        logger.success("NC_FROM_CSV")
        return {"nc_files": [], "errors": []}

    logger.info("NC_FROM_CSV", f"Profiles to convert: {len(files_dict)}")

    nc_files = []
    errors = []

    for profile_num, file_paths in files_dict.items():
        if "science_log" not in file_paths or "system_log" not in file_paths:
            logger.warning("NC_FROM_CSV", f"Profile {profile_num}: missing science_log or system_log — skipping.")
            errors.append(profile_num)
            continue

        nc_path = os.path.join(nc_dir, f"{float_num}-{int(profile_num):03}.nc")
        if os.path.exists(nc_path) and not force_reprocess:
            logger.file_only("NC_FROM_CSV", f"Profile {profile_num}: already exists — skipping.")
            nc_files.append(nc_path)
            continue

        try:
            nc_path = _process_profile(
                profile_num, file_paths, nc_dir, float_num, broken_float,
                to_julian_day, make_nc_file_origin, logger
            )
            nc_files.append(nc_path)
            logger.info("NC_FROM_CSV", f"Profile {profile_num}: success → {os.path.basename(nc_path)}")
        except Exception as e:
            errors.append(profile_num)
            logger.error("NC_FROM_CSV", f"Profile {profile_num}: {e}")

    if not errors:
        logger.success("NC_FROM_CSV")

    return {"nc_files": nc_files, "errors": errors}


# ============================================================================
# Internal: single profile processing (verbatim from read_csv_files)
# ============================================================================

def _process_profile(
    profile_num: str,
    file_paths: dict,
    nc_dir: str,
    float_num: str,
    broken_float: int,
    to_julian_day,
    make_nc_file_origin,
    logger: FloatLogger,
) -> str:
    """
    Process one profile and write the intermediate .nc file.
    Returns the path to the generated .nc file.
    Raises on any error.
    """
    pressures, temps, sals, cndc, temp_cndc, counts, PTSCI_timestamps = [], [], [], [], [], [], []
    latitude, longitude, juld_location, juld_timestamp = None, None, None, None

    # ---- Read science_log.csv ----
    with open(file_paths["science_log"], mode='r') as sci_file:
        reader = csv.reader(sci_file)
        PTSCIinfo = []
        GPS = []
        JULD_timestamp = None
        prev_line = None
        BROKEN_CP_PTSCI_ASCENT_FLAG = False
        BROKEN_PTSCI_DESCENT_FLAG = False

        for row in reader:
            # commented out code pertains to "broken" float F10051, where we played around with taking PTSCI data from the descent
            # if(row[2] == "Park Descent Mission"):
            #     BROKEN_PTSCI_DESCENT_FLAG = True
            # if(row[2] == "Park Mission"):
            #     BROKEN_PTSCI_DESCENT_FLAG = False
            if row[2] == "ASCENT":
                BROKEN_CP_PTSCI_ASCENT_FLAG = True
            # if broken_float == 1:
            #     if BROKEN_PTSCI_DESCENT_FLAG == True:
            #         if row[0] == 'LGR_PTSCI':
            #             # -99 to indicate no vals were avg for this measurement
            #             row.append(-99)
            #             PTSCIinfo.append(row)
            if row[2] == "CP started":
                JULD_timestamp = row[1]
            if broken_float == 1:
                # Different broken float code...
                if BROKEN_CP_PTSCI_ASCENT_FLAG:
                    if row[0] == 'LGR_PTSCI':
                        # -99 to indicate no vals were avg for this measurement
                        row.append(-99)
                        PTSCIinfo.append(row)
                    if row[0] == 'LGR_CP_PTSCI':
                        PTSCIinfo.append(row)
                    if row[2] == "CP started":
                        JULD_timestamp = row[1]
            elif broken_float == 0:
                if row[0] == "LGR_CP_PTSCI":
                    PTSCIinfo.append(row)
            else:
                raise Exception("Please enter a valid number for if this is a broken float")

            # Get lon/lat vals
            if row[0] == 'GPS':
                GPS.append(row)
            if row[2] == "Surface Mission":
                JULD_timestamp = prev_line[1]
            prev_line = row

    # ---- Extract data from PTSCIinfo ----
    for row in PTSCIinfo:
        PTSCI_timestamps.append(int(row[1].replace('T', '')))
        pressures.append(float(row[2]))
        temps.append(float(row[3]))
        sals.append(float(row[4]))
        cndc.append(float(row[5]))
        temp_cndc.append(float(row[6]))
        counts.append(int(row[-1]))

    # ---- GPS / position ----
    try:
        if len(GPS) != 0:
            GPS = GPS[-1]
            latitude = round(float(GPS[2]), 4)
            longitude = round(float(GPS[3]), 4)
            juld_location = to_julian_day(datetime.strptime(GPS[1], "%Y%m%dT%H%M%S"))
        else:
            logger.warning("NC_FROM_CSV", f"Profile {profile_num}: GPS not present.")
            latitude = np.nan
            longitude = np.nan
            juld_location = np.nan
    except (IndexError, ValueError) as e:
        logger.warning("NC_FROM_CSV", f"Profile {profile_num}: Invalid lat/lon: {e}")
        latitude = np.nan
        longitude = np.nan
        juld_location = np.nan

    # ---- JULD timestamp ----
    if JULD_timestamp is not None:
        juld_timestamp = to_julian_day(datetime.strptime(JULD_timestamp, "%Y%m%dT%H%M%S"))
    else:
        if len(PTSCI_timestamps) > 0:
            juld_timestamp = to_julian_day(datetime.strptime(str(PTSCI_timestamps[0]), "%Y%m%d%H%M%S"))
            logger.warning("NC_FROM_CSV", f"Profile {profile_num}: JULD not present; using last PTSCI timestamp.")
        else:
            juld_timestamp = np.nan
            logger.warning("NC_FROM_CSV", f"Profile {profile_num}: JULD not present and no PTSCI timestamps.")

    # ---- Read surface pressure offset from system_log.txt ----
    offset = None
    with open(file_paths["system_log"], mode='r') as sys_file:
        for line in sys_file:
            if 'surface pressure offset' in line:
                offset = line.split(' ')[-2]

    # ---- Reverse arrays to ARGO convention (increasing pressure) ----
    pressures.reverse()
    temps.reverse()
    sals.reverse()
    cndc.reverse()
    temp_cndc.reverse()
    counts.reverse()
    PTSCI_timestamps.reverse()

    # # bin avg data according to pressure - bin size is 2DBAR
    # this is code to bin avg broken float data for F10051
    # bin_edges = np.arange(np.nanmin(pressures), np.nanmax(pressures) + 2, 2)
    # pres_binned = stats.binned_statistic(pressures, pressures, 'mean', bins=bin_edges).statistic
    # temp_binned = stats.binned_statistic(pressures, temps, 'mean', bins=bin_edges).statistic
    # cndc_binned = stats.binned_statistic(pressures, cndc, 'mean', bins=bin_edges).statistic
    # temp_cndc_binned = stats.binned_statistic(pressures, temp_cndc, 'mean', bins=bin_edges).statistic
    # counts_binned = stats.binned_statistic(pressures, pressures, statistic='count', bins=bin_edges).statistic
    # # practical salinity
    # psal_binned = gsw.SP_from_C(cndc_binned, temp_binned, pres_binned)

    # # make sure to reverse data after binning ###
    # pressures = pres_binned
    # temps = temp_binned
    # sals = psal_binned
    # cndc = cndc_binned
    # temp_cndc = temp_cndc_binned
    # counts = counts_binned

    PTSCI_timestamps = np.asarray(PTSCI_timestamps)
    TEMP_ADJUSTED = np.asarray(copy.deepcopy(temps))
    PSAL_ADJUSTED = np.asarray(copy.deepcopy(sals))
    cndc = np.asarray(cndc) / 10  # conductivity scaling

    # ---- Build PRES_ADJUSTED and write .nc ----
    if offset is None:
        logger.warning(
            "NC_FROM_CSV",
            f"Profile {profile_num}: 'surface pressure offset' missing in system_log. "
            "PRES_ADJUSTED initialized as copy of PRES (no offset applied)."
        )
        PRES_ADJUSTED = np.asarray(copy.deepcopy(pressures))
    else:
        PRES_ADJUSTED = np.asarray(copy.deepcopy(pressures)) - float(offset)

    make_nc_file_origin(
        int(profile_num), pressures, temps, sals, cndc, temp_cndc, counts,
        PRES_ADJUSTED, TEMP_ADJUSTED, PSAL_ADJUSTED,
        latitude, longitude, juld_timestamp, juld_location, nc_dir, float_num,
        pres_offset=offset, PTSCI_TIMESTAMPS=PTSCI_timestamps
    )

    nc_path = os.path.join(nc_dir, f"{float_num}-{int(profile_num):03}.nc")
    return nc_path
