import csv
import glob
import os

MIN_LGR_CP_PTSCI = 200
SCIENCE_TYPE = "LGR_CP_PTSCI"


def validate_incomplete_profiles(profiles: list, profiles_dir: str) -> list:
    """
    For each profile marked 'incomplete', check whether the produced CSV files
    meet the scientific data threshold. If they do, upgrade status to 'complete'.

    Criteria for upgrade:
      - All 3 expected files present: science_log.csv, vitals_log.csv, system_log.txt
      - Science CSV has >= 200 rows where column 0 == 'LGR_CP_PTSCI'

    Args:
        profiles:     Aggregated profile list from the state file.
        profiles_dir: Path to PROFILES/ directory containing the CSV/TXT files.

    Returns:
        Updated profiles list (same structure; qualifying 'incomplete' entries → 'complete').
    """
    updated = []
    for profile in profiles:
        if profile.get("status") != "incomplete":
            updated.append(profile)
            continue

        prof_num = profile.get("prof_num", "")
        if _check_profile(prof_num, profiles_dir):
            updated.append({**profile, "status": "complete"})
        else:
            updated.append(profile)
    return updated


def _check_profile(prof_num: str, profiles_dir: str) -> bool:
    """Return True if the profile has all 3 required files and >= 200 LGR_CP_PTSCI rows."""
    science_files = glob.glob(os.path.join(profiles_dir, f"*.{prof_num}.*.science_log.csv"))
    vitals_files  = glob.glob(os.path.join(profiles_dir, f"*.{prof_num}.*.vitals_log.csv"))
    system_files  = glob.glob(os.path.join(profiles_dir, f"*.{prof_num}.*.system_log.txt"))

    if not (science_files and vitals_files and system_files):
        return False

    count = 0
    with open(science_files[0], newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if row and row[0] == SCIENCE_TYPE:
                count += 1

    return count >= MIN_LGR_CP_PTSCI
