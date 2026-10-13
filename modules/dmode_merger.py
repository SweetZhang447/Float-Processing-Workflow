"""
dmode_merger.py — Merge FROM_PROFILE and FROM_ARGO NetCDF sets.

For each profile: prefer FROM_PROFILE if the file exists and is non-empty;
fall back to FROM_ARGO otherwise (e.g. a zero-byte .nc left by an interrupted
Step 5 run). Write results to a merged output directory.
"""
import os
import shutil
from typing import TypedDict

from modules.logger import FloatLogger


class DmodeMergerResult(TypedDict):
    merged_files: list[str]   # full paths of files written to merged dir
    sources: dict[str, str]   # profile_num → "PROFILE" | "RT_ARGO"
    errors: list[str]         # profile_nums with no source available


def run(
    nc_profile_dir: str,
    nc_argo_dir: str,
    nc_merged_dir: str,
    logger: FloatLogger,
    force_reprocess: bool = False,
) -> DmodeMergerResult:
    os.makedirs(nc_merged_dir, exist_ok=True)

    profile_files = {_prof_num(f): f for f in _nc_files(nc_profile_dir)}
    argo_files    = {_prof_num(f): f for f in _nc_files(nc_argo_dir)}
    all_profiles  = sorted(set(profile_files) | set(argo_files))

    merged_files: list[str] = []
    sources: dict[str, str] = {}
    errors: list[str] = []

    for p in all_profiles:
        profile_src = profile_files.get(p)
        if profile_src and _is_empty(profile_src):
            logger.warning("DMODE_MERGE", f"Profile {p}: FROM_PROFILE file is empty — falling back to FROM_ARGO.")
            profile_src = None

        src   = profile_src or argo_files.get(p)
        label = "PROFILE" if p in profile_files else "RT_ARGO"

        if src is None:
            errors.append(p)
            continue

        dest = os.path.join(nc_merged_dir, os.path.basename(src))
        if os.path.exists(dest) and not force_reprocess:
            merged_files.append(dest)
            sources[p] = label
            continue

        shutil.copy2(src, dest)
        merged_files.append(dest)
        sources[p] = label
        logger.info("DMODE_MERGE", f"Profile {p}: {label} → {os.path.basename(dest)}")

    for p in errors:
        logger.warning("DMODE_MERGE", f"Profile {p}: no source file in FROM_PROFILE or FROM_ARGO — skipped.")

    logger.success("DMODE_MERGE")
    return {"merged_files": merged_files, "sources": sources, "errors": errors}


def _nc_files(directory: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    return [os.path.join(directory, f) for f in os.listdir(directory) if f.endswith(".nc")]


def _is_empty(path: str) -> bool:
    return os.path.getsize(path) == 0


def _prof_num(path: str) -> str:
    """Extract zero-padded 3-digit profile number from e.g. '1902655-007.nc'."""
    name = os.path.splitext(os.path.basename(path))[0]
    return name.split("-")[-1].zfill(3)
