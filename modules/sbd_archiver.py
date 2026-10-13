"""
sbd_archiver.py — Keep SBD_FILES/ small by archiving SBDs from complete profiles.

Once a profile is marked "complete" in .sbd_conversion_state.json, its .sbd
files are no longer needed for incremental conversion, so move them into
SBD_FILES/archive/ to speed up directory scans.
"""
import json
import os
import shutil

from tqdm import tqdm

from modules.logger import FloatLogger


def run(sbd_dir: str, profiles_dir: str, logger: FloatLogger) -> list[str]:
    state_file = os.path.join(profiles_dir, ".sbd_conversion_state.json")
    with open(state_file) as f:
        state = json.load(f)

    archive_dir = os.path.join(sbd_dir, "archive")
    os.makedirs(archive_dir, exist_ok=True)

    archived = []
    for profile in tqdm(state["profiles"]):
        if profile["status"] != "complete":
            continue
        start, end = profile["momsn_range"]
        for momsn in range(start, end + 1):
            for name in os.listdir(sbd_dir):
                if name.endswith(f"_{momsn:06d}.sbd"):
                    print(f"archiving {name}")
                    shutil.move(os.path.join(sbd_dir, name), os.path.join(archive_dir, name))
                    archived.append(name)

    # TODO: also archive .sts files
    logger.info("SBD_ARCHIVE", f"Archived {len(archived)} SBD files.")
    return archived
