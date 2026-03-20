"""
nc_from_argo.py — Step 7: Parse real-time ARGO netCDF files to intermediate netCDF format.

Adapted from make_origin_nc_files.py::read_argo_nc_files().

Key changes vs. original:
  - Exposes run() function returning structured result dict.
  - Integrates with FloatLogger for error logging.
  - Per-file try/except: errors (KeyError, IndexError, ValueError, etc.) are logged
    and processing continues to the next file.
  - sys.path injection to import make_nc_file_origin from dmode_tools.

Original logic preserved verbatim:
  - All ARGO variables read: PRES, TEMP, PSAL, CNDC, TEMP_CNDC, NB_SAMPLE_CTD,
    LATITUDE, LONGITUDE, JULD, JULD_LOCATION.
  - All QC arrays read: PSAL_QC, PSAL_ADJUSTED_QC, TEMP_QC, TEMP_ADJUSTED_QC,
    PRES_QC, PRES_ADJUSTED_QC, CNDC_QC, TEMP_CNDC_QC, NB_SAMPLE_CTD_QC, POSITION_QC, JULD_QC.
  - QC defaulting: if array size is 0 or 1, initialize to zeros matching data array shape.
  - Profile number extracted from filename: basename.split("_")[1].split(".nc")[0].
  - Output filename: {float_num}-{profile_num:03}.nc via make_nc_file_origin().
"""

import glob
import os
import sys
from pathlib import Path
from typing import Optional

import netCDF4 as nc4
import numpy as np

from modules.logger import FloatLogger
from modules.nc_from_csv import _configure_dmode_path, _ensure_dmode_imported


# ============================================================================
# Public API
# ============================================================================

def run(
    argo_nc_dir: str,
    nc_dir: str,
    wmo_id: str,
    logger: FloatLogger,
    force_reprocess: bool = False,
) -> dict:
    """
    Parse real-time ARGO netCDF profile files to intermediate netCDF format.

    Args:
        argo_nc_dir:      ARGO_RT_NETCDF_FILES/ directory containing real-time *.nc files.
        nc_dir:           Output directory for intermediate .nc files ({wmo_id}-{profile_num:03}.nc).
        wmo_id:           Float WMO ID used as float_num in output filenames (e.g. '1902655').
        logger:           FloatLogger instance.
        force_reprocess:  If True, regenerate .nc files even if they already exist.

    Returns:
        dict with keys:
            nc_files (list of str — paths of generated .nc files)
            errors   (list of str — filenames that failed)
    """
    try:
        _, make_nc_file_origin = _ensure_dmode_imported()
    except ImportError as e:
        logger.error("NC_FROM_ARGO", str(e))
        return {"nc_files": [], "errors": []}

    os.makedirs(nc_dir, exist_ok=True)

    nc_input_files = glob.glob(os.path.join(argo_nc_dir, "*.nc"))

    if not nc_input_files:
        logger.info("NC_FROM_ARGO", "No .nc files found in ARGO_RT_NETCDF_FILES/ — nothing to convert.")
        logger.success("NC_FROM_ARGO")
        return {"nc_files": [], "errors": []}

    logger.info("NC_FROM_ARGO", f"ARGO .nc files to convert: {len(nc_input_files)}")

    nc_output_files = []
    errors = []

    for file in nc_input_files:
        fname = os.path.basename(file)
        try:
            profile_num = int(fname.split("_")[1].split(".nc")[0])
            nc_path = os.path.join(nc_dir, f"{wmo_id}-{profile_num:03}.nc")
        except (IndexError, ValueError):
            nc_path = None

        if nc_path and os.path.exists(nc_path) and not force_reprocess:
            logger.file_only("NC_FROM_ARGO", f"{fname}: already exists — skipping.")
            nc_output_files.append(nc_path)
            continue

        try:
            nc_path = _process_argo_nc(file, nc_dir, wmo_id, make_nc_file_origin)
            nc_output_files.append(nc_path)
            logger.info("NC_FROM_ARGO", f"{fname}: success → {os.path.basename(nc_path)}")
        except Exception as e:
            errors.append(fname)
            logger.error("NC_FROM_ARGO", f"{fname}: {type(e).__name__}: {e}")

    if not errors:
        logger.success("NC_FROM_ARGO")

    return {"nc_files": nc_output_files, "errors": errors}


# ============================================================================
# Internal: single file processing (verbatim from read_argo_nc_files)
# ============================================================================

def _qc_array(raw_qc, reference_shape):
    """
    Squeeze and validate a QC array. Returns integer QC array or zeros if
    array is size 0 or 1 (missing or scalar placeholder).
    Preserves original QC defaulting logic from read_argo_nc_files.
    """
    squeezed = np.squeeze(raw_qc.filled(np.nan) if hasattr(raw_qc, 'filled') else raw_qc)
    if squeezed.size <= 1:
        return np.full(reference_shape, fill_value=0)
    return np.asarray([int(x) for x in squeezed])


def _process_argo_nc(file: str, nc_dir: str, float_num: str, make_nc_file_origin) -> str:
    """
    Process one ARGO real-time .nc file and write an intermediate .nc file.
    Returns the path to the generated .nc file.
    Raises on any error.
    """
    nc = nc4.Dataset(file)

    try:
        profile_num = int(os.path.basename(file).split("_")[1].split(".nc")[0])

        pressures = np.squeeze(nc.variables['PRES'][:].filled(np.nan))
        temps = np.squeeze(nc.variables['TEMP'][:].filled(np.nan))
        sals = np.squeeze(nc.variables['PSAL'][:].filled(np.nan))
        cndc = np.squeeze(nc.variables['CNDC'][:].filled(np.nan))
        temp_cndc = np.squeeze(nc.variables['TEMP_CNDC'][:].filled(np.nan))
        counts = nc.variables['NB_SAMPLE_CTD'][:].filled(-99)
        latitude = nc.variables['LATITUDE'][:].filled(np.nan)
        longitude = nc.variables['LONGITUDE'][:].filled(np.nan)
        juld_timestamp = nc.variables['JULD'][:].filled(np.nan)
        juld_location = nc.variables['JULD_LOCATION'][:].filled(np.nan)

        PSAL_ADJUSTED = np.squeeze(nc.variables['PSAL_ADJUSTED'][:].filled(np.nan))
        PSAL_ADJUSTED_QC = _qc_array(nc.variables['PSAL_ADJUSTED_QC'][:], sals.shape)
        PSAL_QC = _qc_array(nc.variables['PSAL_QC'][:], sals.shape)

        TEMP_ADJUSTED = np.squeeze(nc.variables['TEMP_ADJUSTED'][:].filled(np.nan))
        TEMP_ADJUSTED_QC = _qc_array(nc.variables['TEMP_ADJUSTED_QC'][:], temps.shape)
        TEMP_QC = _qc_array(nc.variables['TEMP_QC'][:], temps.shape)

        PRES_ADJUSTED = np.squeeze(nc.variables['PRES_ADJUSTED'][:].filled(np.nan))
        PRES_ADJUSTED_QC = _qc_array(nc.variables['PRES_ADJUSTED_QC'][:], pressures.shape)
        PRES_QC = _qc_array(nc.variables['PRES_QC'][:], pressures.shape)

        CNDC_QC = _qc_array(nc.variables['CNDC_QC'][:], pressures.shape)

        POSITION_QC = int(nc.variables['POSITION_QC'][:].filled(np.nan)[0])
        JULD_QC = int(nc.variables['JULD_QC'][:].filled(np.nan)[0])

        TEMP_CNDC_QC = _qc_array(nc.variables['TEMP_CNDC_QC'][:], pressures.shape)
        NB_SAMPLE_CTD_QC = _qc_array(nc.variables['NB_SAMPLE_CTD_QC'][:], pressures.shape)

    finally:
        nc.close()

    make_nc_file_origin(
        profile_num, pressures, temps, sals, cndc, temp_cndc, counts,
        PRES_ADJUSTED, TEMP_ADJUSTED, PSAL_ADJUSTED,
        latitude, longitude, juld_timestamp, juld_location, nc_dir, float_num,
        PSAL_QC=PSAL_QC,
        TEMP_QC=TEMP_QC,
        PRES_QC=PRES_QC,
        CNDC_QC=CNDC_QC,
        PSAL_ADJUSTED_QC=PSAL_ADJUSTED_QC,
        TEMP_ADJUSTED_QC=TEMP_ADJUSTED_QC,
        PRES_ADJUSTED_QC=PRES_ADJUSTED_QC,
        POSITION_QC=POSITION_QC,
        JULD_QC=JULD_QC,
        TEMP_CNDC_QC=TEMP_CNDC_QC,
        NB_SAMPLE_CTD_QC=NB_SAMPLE_CTD_QC,
    )

    return os.path.join(nc_dir, f"{float_num}-{profile_num:03}.nc")
