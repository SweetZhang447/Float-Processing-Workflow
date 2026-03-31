"""
dmode_tools.py — Core DMODE utility functions used by nc_from_csv and nc_from_argo.

Copied verbatim from:
  - DMODE_processing/dmode_tools/tools.py           (from_julian_day, to_julian_day,
                                                      del_all_nan_slices,
                                                      make_intermediate_nc_file,
                                                      read_intermediate_nc_file)
  - DMODE_processing/dmode_tools/make_origin_nc_files.py  (make_nc_file_origin)

No sys.path injection — functions are imported directly from this module.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union
import itertools
import glob
import os

import netCDF4 as nc4
import numpy as np


# ============================================================================
# Julian day utilities  (from tools.py)
# ============================================================================

def from_julian_day(julian_day: float) -> Union[datetime, float]:
    """
    Convert a Julian day (referenced to 1950-01-01 00:00:00 UTC) to a datetime object.

    Parameters
    ----------
    julian_day : float
        Days since 1950-01-01 00:00:00 UTC.

    Returns
    -------
    datetime or float
        Timezone-aware datetime (UTC) if julian_day is not NaN; NaN otherwise.
    """
    julian_day = np.float64(julian_day)

    if not np.isnan(julian_day):
        reference_date = datetime(1950, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        delta = timedelta(days=julian_day)
        dt = reference_date + delta
        return dt
    else:
        return julian_day


def to_julian_day(date_obj: datetime) -> float:
    """
    Convert a datetime object to a Julian day referenced to 1950-01-01 00:00:00 UTC.

    Parameters
    ----------
    date_obj : datetime
        The date/time to convert. If timezone-naive, assumed UTC.

    Returns
    -------
    float
        Days since 1950-01-01 00:00:00 UTC (fractional).
    """
    if date_obj.tzinfo is None:
        date_obj = date_obj.replace(tzinfo=timezone.utc)

    delta = date_obj - datetime(1950, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    julian_day = delta.total_seconds() / 86400
    return julian_day


def del_all_nan_slices(argo_data: dict[str, Any]) -> dict[str, Any]:
    """
    Remove profiles where PRES, TEMP, or PSAL ADJUSTED arrays are entirely NaN.

    A profile is excluded if ALL depth levels are NaN in any of the three key
    adjusted arrays. Modifies both 2D data arrays and 1D metadata arrays
    (PROFILE_NUMS, LATs, LONs, JULDs) in place on the returned dict.

    Parameters
    ----------
    argo_data : dict
        Intermediate netCDF data dict from read_intermediate_nc_file().
        Must contain PRES_ADJUSTED, TEMP_ADJUSTED, PSAL_ADJUSTED (2D),
        and PROFILE_NUMS, LATs, LONs, JULDs (1D).

    Returns
    -------
    argo_data : dict
        Same dict with all-NaN profiles removed from all arrays.
    """
    pres_mask = np.isnan(argo_data["PRES_ADJUSTED"]).all(axis=1)
    temp_mask = np.isnan(argo_data["TEMP_ADJUSTED"]).all(axis=1)
    psal_mask = np.isnan(argo_data["PSAL_ADJUSTED"]).all(axis=1)
    bad_vals_mask = ~(pres_mask | temp_mask | psal_mask)

    argo_data["PRES_ADJUSTED"] = argo_data["PRES_ADJUSTED"][bad_vals_mask]
    argo_data["TEMP_ADJUSTED"] = argo_data["TEMP_ADJUSTED"][bad_vals_mask]
    argo_data["PSAL_ADJUSTED"] = argo_data["PSAL_ADJUSTED"][bad_vals_mask]

    single_dim_bad_vals_mask = np.where(bad_vals_mask == True)
    argo_data["PROFILE_NUMS"] = argo_data["PROFILE_NUMS"][single_dim_bad_vals_mask]
    argo_data["LATs"] = argo_data["LATs"][single_dim_bad_vals_mask]
    argo_data["LONs"] = argo_data["LONs"][single_dim_bad_vals_mask]
    argo_data["JULDs"] = argo_data["JULDs"][single_dim_bad_vals_mask]

    return argo_data


def make_intermediate_nc_file(argo_data: dict[str, Any], dest_filepath: str, float_num: str, profile_num: Optional[int] = None) -> None:
    """
    Write intermediate netCDF files from an argo_data dict.

    Each profile is written to a separate file named {float_num}-{profile_num:03}.nc.
    Trailing NaN levels (beyond the last valid PRES value) are stripped before writing.
    Called by delayed_mode_processing.py after QC modifications.

    Parameters
    ----------
    argo_data : dict
        Intermediate netCDF data dict (from read_intermediate_nc_file() or manually built).
        Must contain all standard keys: PRESs, TEMPs, PSALs, CNDCs, TEMP_CNDCs,
        TEMP_CNDC_QC, PRES_OFFSET, NB_SAMPLE_CTD, NB_SAMPLE_CTD_QC, JULDs,
        JULD_LOCATIONs, LATs, LONs, POSITION_QC, JULD_QC, PSAL_ADJUSTED,
        PSAL_ADJUSTED_QC, TEMP_ADJUSTED, TEMP_ADJUSTED_QC, PRES_ADJUSTED,
        PRES_ADJUSTED_QC, PSAL_QC, TEMP_QC, PRES_QC, CNDC_QC, PTSCI_TIMESTAMPS,
        and PROFILE_NUMS.
    dest_filepath : str
        Output directory where .nc files are written.
    float_num : str
        Float identifier used in the output filename (e.g. 'F9186').
    profile_num : int, optional
        If provided, only write the file for this specific profile number.
        If None (default), write files for all profiles in argo_data.
    """
    if profile_num is None:
        iterate_len_profile_nums = np.arange(len(argo_data["PROFILE_NUMS"]))
    else:
        iterate_len_profile_nums = [0]

    for i in iterate_len_profile_nums:

        if profile_num is None:
            prof_num = int(argo_data["PROFILE_NUMS"][i])
        else:
            i = np.where(argo_data["PROFILE_NUMS"] == profile_num)[0][0]
            prof_num = int(profile_num)

        output_filename = os.path.join(dest_filepath, f"{float_num}-{prof_num:03}.nc")
        nc = nc4.Dataset(output_filename, 'w')

        nc.author = 'Sweet Zhang'

        nan_index = np.where(~np.isnan(argo_data["PRESs"][i, :]))[0][-1] + 1

        length = len(argo_data["PRESs"][i, :nan_index])
        record_dim = nc.createDimension('records', length)
        single_dim = nc.createDimension('single_record', 1)

        profile_nums_var = nc.createVariable('PROFILE_NUM', 'f4', 'single_record')
        profile_nums_var[:] = prof_num

        pressure_var = nc.createVariable('PRES', 'f4', 'records')
        pressure_var.units = 'DBAR'
        pressure_var[:] = argo_data["PRESs"][i, :nan_index]

        temperature_var = nc.createVariable('TEMP', 'f4', 'records')
        temperature_var.units = 'CELSIUS'
        temperature_var[:] = argo_data["TEMPs"][i, :nan_index]

        salinity_var = nc.createVariable('PSAL', 'f4', 'records')
        salinity_var.units = 'PSU'
        salinity_var[:] = argo_data["PSALs"][i, :nan_index]

        cndc_var = nc.createVariable('CNDC', 'f4', 'records')
        cndc_var.units = "mhos/m"
        cndc_var[:] = argo_data["CNDCs"][i, :nan_index]

        temp_cndc_var = nc.createVariable('TEMP_CNDC', 'f4', 'records')
        temp_cndc_var.units = 'degree_celsius'
        temp_cndc_var[:] = argo_data["TEMP_CNDCs"][i, :nan_index]

        temp_cndc_qc_var = nc.createVariable('TEMP_CNDC_QC', 'f4', 'records')
        temp_cndc_qc_var[:] = argo_data["TEMP_CNDC_QC"][i, :nan_index]

        offset_var = nc.createVariable('PRES_OFFSET', 'f4', 'single_record')
        offset_var[:] = argo_data["PRES_OFFSET"][i]

        counts_var = nc.createVariable('NB_SAMPLE_CTD', 'f4', 'records')
        counts_var[:] = argo_data["NB_SAMPLE_CTD"][i, :nan_index]

        counts_qc_var = nc.createVariable('NB_SAMPLE_CTD_QC', 'f4', 'records')
        counts_qc_var[:] = argo_data["NB_SAMPLE_CTD_QC"][i, :nan_index]

        juld_var = nc.createVariable('JULD', 'f4', 'single_record')
        juld_var[:] = argo_data["JULDs"][i]

        juld_location_var = nc.createVariable('JULD_LOCATION', 'f4', 'single_record')
        juld_location_var[:] = argo_data["JULD_LOCATIONs"][i]

        lat_var = nc.createVariable('LAT', 'f4', 'single_record')
        lat_var[:] = argo_data["LATs"][i]

        lon_var = nc.createVariable('LON', 'f4', 'single_record')
        lon_var[:] = argo_data["LONs"][i]

        POSITION_QC_var = nc.createVariable('POSITION_QC', 'f4', 'single_record')
        POSITION_QC_var[:] = argo_data["POSITION_QC"][i]

        JULD_QC_var = nc.createVariable('JULD_QC', 'f4', 'single_record')
        JULD_QC_var[:] = argo_data["JULD_QC"][i]

        PSAL_ADJUSTED_VAR = nc.createVariable('PSAL_ADJUSTED', 'f4', 'records')
        PSAL_ADJUSTED_VAR[:] = argo_data["PSAL_ADJUSTED"][i, :nan_index]

        PSAL_ADJUSTED_QC_VAR = nc.createVariable('PSAL_ADJUSTED_QC', 'f4', 'records')
        PSAL_ADJUSTED_QC_VAR[:] = argo_data["PSAL_ADJUSTED_QC"][i, :nan_index]

        TEMP_ADJUSTED_VAR = nc.createVariable('TEMP_ADJUSTED', 'f4', 'records')
        TEMP_ADJUSTED_VAR[:] = argo_data["TEMP_ADJUSTED"][i, :nan_index]

        TEMP_ADJUSTED_QC_VAR = nc.createVariable('TEMP_ADJUSTED_QC', 'f4', 'records')
        TEMP_ADJUSTED_QC_VAR[:] = argo_data["TEMP_ADJUSTED_QC"][i, :nan_index]

        PRES_ADJUSTED_VAR = nc.createVariable('PRES_ADJUSTED', 'f4', 'records')
        PRES_ADJUSTED_VAR[:] = argo_data["PRES_ADJUSTED"][i, :nan_index]

        PRES_ADJUSTED_QC_VAR = nc.createVariable('PRES_ADJUSTED_QC', 'f4', 'records')
        PRES_ADJUSTED_QC_VAR[:] = argo_data["PRES_ADJUSTED_QC"][i, :nan_index]

        PSAL_QC_VAR = nc.createVariable('PSAL_QC', 'f4', 'records')
        PSAL_QC_VAR[:] = argo_data["PSAL_QC"][i, :nan_index]

        TEMP_QC_VAR = nc.createVariable('TEMP_QC', 'f4', 'records')
        TEMP_QC_VAR[:] = argo_data["TEMP_QC"][i, :nan_index]

        PRES_QC_VAR = nc.createVariable('PRES_QC', 'f4', 'records')
        PRES_QC_VAR[:] = argo_data["PRES_QC"][i, :nan_index]

        CNDC_QC_VAR = nc.createVariable('CNDC_QC', 'f4', 'records')
        CNDC_QC_VAR[:] = argo_data["CNDC_QC"][i, :nan_index]

        ptsci_timestamps_var = nc.createVariable('PTSCI_TIMESTAMPS', 'i8', 'records')
        ptsci_timestamps_var.long_name = "Format: YYYYMMDDHHMMSS"
        argo_data["PTSCI_TIMESTAMPS"][i, :nan_index][np.isnan(argo_data["PTSCI_TIMESTAMPS"][i, :nan_index])] = 0
        ptsci_timestamps_var[:] = argo_data["PTSCI_TIMESTAMPS"][i, :nan_index]

        nc.close()


def read_intermediate_nc_file(filepath: str) -> dict[str, Any]:
    """
    Read all intermediate netCDF files in a directory into a single argo_data dict.

    Files are sorted and loaded in order. Profiles with fewer than 3 valid depth
    levels in PRES, PSAL, or TEMP are skipped. Arrays with varying profile lengths
    are padded to the longest profile using NaN (via itertools.zip_longest).

    Parameters
    ----------
    filepath : str
        Directory containing intermediate .nc files (e.g., 'F9186-001.nc', ...).

    Returns
    -------
    argo_data : dict
        All profiles combined into 2D arrays (n_profiles, n_levels) for depth
        variables and 1D arrays (n_profiles,) for profile-level scalars.
    """
    argo_keys = [
        "CNDC", "CNDC_QC",
        "JULD", "JULD_LOCATION", "JULD_QC", "LAT", "LON", "NB_SAMPLE_CTD", "NB_SAMPLE_CTD_QC", "POSITION_QC",
        "PRES", "PRES_ADJUSTED", "PRES_ADJUSTED_QC", "PRES_OFFSET", "PRES_QC",
        "PROFILE_NUMS",
        "PSAL", "PSAL_ADJUSTED", "PSAL_ADJUSTED_QC", "PSAL_QC",
        "TEMP", "TEMP_ADJUSTED", "TEMP_ADJUSTED_QC", "TEMP_CNDC", "TEMP_CNDC_QC", "TEMP_QC",
        "PTSCI_TIMESTAMPS"
    ]

    argo_data = {key: [] for key in argo_keys}

    files = sorted(glob.glob(os.path.join(filepath, "*.nc")))

    for f in files:
        float_dataset = nc4.Dataset(f)

        PRES_temp = np.squeeze(float_dataset.variables['PRES'][:].filled(np.nan))
        PSAL_temp = np.squeeze(float_dataset.variables['PSAL'][:].filled(np.nan))
        TEMP_temp = np.squeeze(float_dataset.variables['TEMP'][:].filled(np.nan))

        if PRES_temp.size <= 2 or PSAL_temp.size <= 2 or TEMP_temp.size <= 2:
            print(f"Skipping file: {os.path.basename(f)} due to missing data")
        else:
            for key in argo_keys:
                if key in float_dataset.variables:
                    argo_data[key].append(float_dataset.variables[key][:].filled(np.nan))
                elif key == "PROFILE_NUMS":
                    argo_data[key].append(int(float_dataset.variables['PROFILE_NUM'][:].filled(np.nan)[0]))

        float_dataset.close()

    for key in argo_keys:
        if key in ["CNDC", "CNDC_QC",
                   "NB_SAMPLE_CTD", "NB_SAMPLE_CTD_QC",
                   "PRES", "PRES_ADJUSTED", "PRES_ADJUSTED_QC", "PRES_QC",
                   "PSAL", "PSAL_ADJUSTED", "PSAL_ADJUSTED_QC", "PSAL_QC",
                   "TEMP", "TEMP_ADJUSTED", "TEMP_ADJUSTED_QC", "TEMP_CNDC", "TEMP_CNDC_QC", "TEMP_QC",
                   "PTSCI_TIMESTAMPS"
                   ]:
            argo_data[key] = np.squeeze(
                np.array(list(itertools.zip_longest(*argo_data[key], fillvalue=np.nan))).T
            )
        else:
            argo_data[key] = np.squeeze(np.array(argo_data[key]))

    rename_keys = ["CNDCs", "JULDs", "JULD_LOCATIONs", "LATs", "LONs",
                   "PRESs", "PSALs", "TEMPs", "TEMP_CNDCs"]
    for a in rename_keys:
        temp_keyname = a[:-1]
        argo_data[a] = argo_data.pop(temp_keyname)

    return argo_data


# ============================================================================
# NC file writer  (from make_origin_nc_files.py)
# ============================================================================

def make_nc_file_origin(profile_num: int, pressures: Any, temps: Any, sals: Any, cndc: Any, temp_cndc: Any, counts: Any,
                        PRES_ADJUSTED: Any, TEMP_ADJUSTED: Any, PSAL_ADJUSTED: Any,
                        latitude: float, longitude: float, juld_timestamp: float, juld_location: float, dest_filepath: str, float_num: str,
                        **kwargs: Any) -> None:
    """
    Write one intermediate netCDF file for a single profile.

    Creates a file at dest_filepath/{float_num}-{profile_num:03}.nc with the
    standard intermediate netCDF dimensions (records, single_record) and all
    required variables. Any QC arrays not provided via kwargs are initialized to
    arrays of zeros (no QC applied).

    Parameters
    ----------
    profile_num : int
        Profile number, used in the filename and stored as PROFILE_NUM.
    pressures, temps, sals, cndc, temp_cndc : array-like
        Raw sensor data arrays (depth levels).
    counts : array-like
        NB_SAMPLE_CTD bin-average sample counts per depth level.
    PRES_ADJUSTED, TEMP_ADJUSTED, PSAL_ADJUSTED : array-like
        Adjusted data arrays (same shape as raw; PRES_ADJUSTED = PRES - surface offset).
    latitude, longitude : float
        Profile position.
    juld_timestamp : float
        Julian day of the profile (1950-01-01 reference).
    juld_location : float
        Julian day of the GPS position fix.
    dest_filepath : str
        Output directory.
    float_num : str
        Float identifier (e.g. 'F9186').
    **kwargs : optional
        QC and metadata arrays to include. If not provided, initialized to zeros:
        PRES_QC, TEMP_QC, PSAL_QC, CNDC_QC, TEMP_CNDC_QC, NB_SAMPLE_CTD_QC,
        PSAL_ADJUSTED_QC, TEMP_ADJUSTED_QC, PRES_ADJUSTED_QC, POSITION_QC,
        JULD_QC, PRES_OFFSET, PTSCI_TIMESTAMPS.
    """
    output_filename = os.path.join(dest_filepath, f"{float_num}-{profile_num:03}.nc")
    nc = nc4.Dataset(output_filename, 'w')

    nc.author = 'Sweet Zhang'
    nc.summary = 'NC file: LGR_CP_PTSCI readings ONLY'

    pressures = np.asarray(pressures)
    temps = np.asarray(temps)
    sals = np.asarray(sals)
    cndc = np.asarray(cndc)
    temp_cndc = np.asarray(temp_cndc)
    counts = np.asarray(counts)

    PSAL_ADJUSTED_QC = kwargs.get("PSAL_ADJUSTED_QC", np.full(sals.shape, fill_value=0))
    TEMP_ADJUSTED_QC = kwargs.get("TEMP_ADJUSTED_QC", np.full(temps.shape, fill_value=0))
    PRES_ADJUSTED_QC = kwargs.get("PRES_ADJUSTED_QC", np.full(pressures.shape, fill_value=0))

    PSAL_QC = kwargs.get("PSAL_QC", np.full(pressures.shape, fill_value=0))
    TEMP_QC = kwargs.get("TEMP_QC", np.full(pressures.shape, fill_value=0))
    PRES_QC = kwargs.get("PRES_QC", np.full(pressures.shape, fill_value=0))
    CNDC_QC = kwargs.get("CNDC_QC", np.full(pressures.shape, fill_value=0))

    TEMP_CNDC_QC = kwargs.get("TEMP_CNDC_QC", np.full(pressures.shape, fill_value=0))
    NB_SAMPLE_CTD_QC = kwargs.get("NB_SAMPLE_CTD_QC", np.full(pressures.shape, fill_value=0))
    POSITION_QC = kwargs.get("POSITION_QC", np.nan)
    JULD_QC = kwargs.get("JULD_QC", np.nan)
    offset = kwargs.get("pres_offset", None)
    PTSCI_TIMESTAMPS = kwargs.get("PTSCI_TIMESTAMPS", np.full(pressures.shape, fill_value=0))

    length = pressures.size
    record_dim = nc.createDimension('records', length)
    lat_dim = nc.createDimension('single_record', 1)

    profile_nums_var = nc.createVariable('PROFILE_NUM', 'f4', 'single_record')
    profile_nums_var[:] = profile_num

    pressure_var = nc.createVariable('PRES', 'f4', 'records')
    pressure_var.units = 'DBAR'
    pressure_var[:] = pressures

    temperature_var = nc.createVariable('TEMP', 'f4', 'records')
    temperature_var.units = 'CELSIUS'
    temperature_var[:] = temps

    salinity_var = nc.createVariable('PSAL', 'f4', 'records')
    salinity_var.units = 'PSU'
    salinity_var[:] = sals

    cndc_var = nc.createVariable('CNDC', 'f4', 'records')
    cndc_var.units = "mhos/m"
    cndc_var[:] = cndc

    temp_cndc_var = nc.createVariable('TEMP_CNDC', 'f4', 'records')
    temp_cndc_var.units = 'degree_celsius'
    temp_cndc_var[:] = temp_cndc

    temp_cndc_qc_var = nc.createVariable('TEMP_CNDC_QC', 'f4', 'records')
    temp_cndc_qc_var[:] = TEMP_CNDC_QC

    offset_var = nc.createVariable('PRES_OFFSET', 'f4', 'single_record')
    if offset is None:
        offset_var[:] = int(-9999)
    else:
        offset_var[:] = offset

    ptsci_timestamps_var = nc.createVariable('PTSCI_TIMESTAMPS', 'i8', 'records')
    ptsci_timestamps_var.long_name = "Format: YYYYMMDDHHMMSS"
    ptsci_timestamps_var[:] = PTSCI_TIMESTAMPS

    counts_var = nc.createVariable('NB_SAMPLE_CTD', 'f4', 'records')
    counts_var[:] = counts

    counts_qc_var = nc.createVariable('NB_SAMPLE_CTD_QC', 'f4', 'records')
    counts_qc_var[:] = NB_SAMPLE_CTD_QC

    juld_var = nc.createVariable('JULD', 'f4', 'single_record')
    juld_var[:] = juld_timestamp

    juld_location_var = nc.createVariable('JULD_LOCATION', 'f4', 'single_record')
    juld_location_var[:] = juld_location

    lat_var = nc.createVariable('LAT', 'f4', 'single_record')
    lat_var[:] = latitude

    lon_var = nc.createVariable('LON', 'f4', 'single_record')
    lon_var[:] = longitude

    POSITION_QC_var = nc.createVariable('POSITION_QC', 'f4', 'single_record')
    POSITION_QC_var[:] = POSITION_QC

    JULD_QC_var = nc.createVariable('JULD_QC', 'f4', 'single_record')
    JULD_QC_var[:] = JULD_QC

    PSAL_ADJUSTED_VAR = nc.createVariable('PSAL_ADJUSTED', 'f4', 'records')
    PSAL_ADJUSTED_VAR[:] = PSAL_ADJUSTED

    PSAL_ADJUSTED_QC_VAR = nc.createVariable('PSAL_ADJUSTED_QC', 'f4', 'records')
    PSAL_ADJUSTED_QC_VAR[:] = PSAL_ADJUSTED_QC

    TEMP_ADJUSTED_VAR = nc.createVariable('TEMP_ADJUSTED', 'f4', 'records')
    TEMP_ADJUSTED_VAR[:] = TEMP_ADJUSTED

    TEMP_ADJUSTED_QC_VAR = nc.createVariable('TEMP_ADJUSTED_QC', 'f4', 'records')
    TEMP_ADJUSTED_QC_VAR[:] = TEMP_ADJUSTED_QC

    PRES_ADJUSTED_VAR = nc.createVariable('PRES_ADJUSTED', 'f4', 'records')
    PRES_ADJUSTED_VAR[:] = PRES_ADJUSTED

    PRES_ADJUSTED_QC_VAR = nc.createVariable('PRES_ADJUSTED_QC', 'f4', 'records')
    PRES_ADJUSTED_QC_VAR[:] = PRES_ADJUSTED_QC

    PSAL_QC_VAR = nc.createVariable('PSAL_QC', 'f4', 'records')
    PSAL_QC_VAR[:] = PSAL_QC

    TEMP_QC_VAR = nc.createVariable('TEMP_QC', 'f4', 'records')
    TEMP_QC_VAR[:] = TEMP_QC

    PRES_QC_VAR = nc.createVariable('PRES_QC', 'f4', 'records')
    PRES_QC_VAR[:] = PRES_QC

    CNDC_QC_VAR = nc.createVariable('CNDC_QC', 'f4', 'records')
    CNDC_QC_VAR[:] = CNDC_QC

    nc.close()
