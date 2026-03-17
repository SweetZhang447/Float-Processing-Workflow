"""
workflow.py — ARGO Float Automated Processing Pipeline Orchestrator.

Usage:
    # Run once immediately:
    python workflow.py

    # Run once with a specific config:
    python workflow.py --config path/to/config.json

    # Run on an automated schedule (daily/weekly/monthly as set in config.json):
    python workflow.py --schedule

    # Interactive Gmail auth setup (run once manually before scheduling):
    python workflow.py --setup-gmail-auth

Pipeline steps (per float):
    1. SBD download       — Gmail → SBD_FILES/
    2. Profile conversion — SBD_FILES/ → PROFILES/  (.gz and .bin deleted)
    3. PHY parsing        — PROFILES/ → PHY_FILES/
    4. NC from CSV        — PROFILES/ → DMODE/{float_id}_0/{float_id}_0_FROM_PROFILE/
    5. ARGO RT download   — ARGO data center → ARGO_RT_NETCDF_FILES/
    6. NC from ARGO       — ARGO_RT_NETCDF_FILES/ → DMODE/{float_id}_0/{float_id}_0_FROM_ARGO/

All output under {output_base_dir}/{float_id}/.
Master status tracked in {output_base_dir}/master_status.json.
Per-run timestamped log written to {output_base_dir}/{float_id}/Logs/.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ============================================================================
# Lazy imports (deferred so --setup-gmail-auth doesn't need APScheduler etc.)
# ============================================================================

def _import_scheduler():
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
        return BlockingScheduler, CronTrigger
    except ImportError:
        print("ERROR: APScheduler not installed. Run: pip install apscheduler")
        sys.exit(1)


# ============================================================================
# Config loading
# ============================================================================

DEFAULT_CONFIG = os.path.join(os.path.dirname(__file__), "config.json")


def load_config(config_path: str) -> dict:
    with open(config_path, "r") as f:
        raw = json.load(f)
    # Strip _comment keys
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _strip_comments(obj):
    """Recursively remove keys starting with '_comment' from config dicts."""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(item) for item in obj]
    return obj


def load_config(config_path: str) -> dict:
    with open(config_path, "r") as f:
        raw = json.load(f)
    return _strip_comments(raw)


# ============================================================================
# Master status file
# ============================================================================

def load_master_status(output_base_dir: str) -> dict:
    path = os.path.join(output_base_dir, "master_status.json")
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {"last_sbd_download_date": None, "floats": {}}


def save_master_status(output_base_dir: str, status: dict):
    path = os.path.join(output_base_dir, "master_status.json")
    with open(path, "w") as f:
        json.dump(status, f, indent=2)


def _update_profile_status(master_status: dict, float_id: str, profile_num: str, step: str, result: str):
    master_status["floats"].setdefault(float_id, {"profiles": {}, "argo_rt": {}})
    master_status["floats"][float_id]["profiles"].setdefault(profile_num, {})
    master_status["floats"][float_id]["profiles"][profile_num][step] = result


def _update_argo_rt_status(master_status: dict, float_id: str, key: str, value):
    master_status["floats"].setdefault(float_id, {"profiles": {}, "argo_rt": {}})
    master_status["floats"][float_id]["argo_rt"][key] = value


# ============================================================================
# Directory setup
# ============================================================================

def _ensure_float_dirs(float_dir: str, float_id: str):
    """Create all required subdirectories under the float's output directory."""
    subdirs = [
        "Logs",
        "SBD_FILES",
        "PROFILES",
        "PHY_FILES",
        "ARGO_RT_NETCDF_FILES",
        os.path.join("DMODE", f"{float_id}_0", f"{float_id}_0_FROM_PROFILE"),
        os.path.join("DMODE", f"{float_id}_0", f"{float_id}_0_FROM_ARGO"),
    ]
    for sub in subdirs:
        os.makedirs(os.path.join(float_dir, sub), exist_ok=True)


# ============================================================================
# Per-float pipeline
# ============================================================================

def run_float_pipeline(
    float_cfg: dict,
    gmail_cfg: dict,
    output_base_dir: str,
    master_status: dict,
    run_time: datetime,
    gmail_client=None,
    dmode_tools_path: Optional[str] = None,
    force_reprocess: bool = False,
):
    """
    Run the full 7-step pipeline for one float.

    Args:
        float_cfg:        Float config dict (from config.json floats[]).
        gmail_cfg:        Gmail config dict (from config.json gmail).
        output_base_dir:  Root output directory.
        master_status:    Mutable master status dict (updated in-place).
        run_time:         UTC datetime of this run (used for log filename).
        gmail_client:     Optional pre-built GmailApi (shared auth across floats).
        dmode_tools_path: Optional override for dmode_tools sys.path injection.
        force_reprocess:  If True, regenerate all intermediate files even if they exist.
    """
    from modules.logger import FloatLogger
    from modules import sbd_downloader, sbd_converter, phy_parser
    from modules import nc_from_csv, argo_downloader, nc_from_argo

    float_id = float_cfg["float_id"]
    float_dir = os.path.join(output_base_dir, float_id)
    _ensure_float_dirs(float_dir, float_id)

    logger = FloatLogger(float_id=float_id, float_dir=float_dir, run_time=run_time)
    logger.info("OVERALL_SUMMARY", f"Pipeline started for {float_id} at {run_time.isoformat()}Z")

    # Configure dmode_tools path if specified in config
    if dmode_tools_path:
        nc_from_csv._configure_dmode_path(dmode_tools_path)

    # -------------------------------------------------------------------------
    # Step 1: SBD Download
    # -------------------------------------------------------------------------
    sbd_result = sbd_downloader.run(
        float_cfg=float_cfg,
        gmail_cfg=gmail_cfg,
        float_dir=float_dir,
        logger=logger,
        client=gmail_client,
    )
    master_status["last_sbd_download_date"] = run_time.isoformat()

    # -------------------------------------------------------------------------
    # Step 2 & 3: SBD → Profiles (SBD_FILES → PROFILES; .gz and .bin deleted)
    # -------------------------------------------------------------------------
    sbd_dir = os.path.join(float_dir, "SBD_FILES")
    profiles_dir = os.path.join(float_dir, "PROFILES")

    conv_result = sbd_converter.run(
        sbd_dir=sbd_dir,
        profiles_dir=profiles_dir,
        float_id=float_id,
        logger=logger,
        force_reprocess=force_reprocess,
    )

    # Update master status per profile
    for p in conv_result["converted_profiles"]:
        _update_profile_status(master_status, float_id, p, "profile_conversion", "success")
    for p in conv_result["failed_profiles"]:
        _update_profile_status(master_status, float_id, p, "profile_conversion",
                               "failure: incomplete .gz (likely mid-transmission)")

    # -------------------------------------------------------------------------
    # Step 4: PHY parsing (PROFILES → PHY_FILES)
    # -------------------------------------------------------------------------
    phy_dir = os.path.join(float_dir, "PHY_FILES")
    meta_file = float_cfg.get("meta_file") or ""

    phy_result = phy_parser.run(
        profiles_dir=profiles_dir,
        phy_dir=phy_dir,
        float_id=float_id,
        aoml_id=str(float_cfg.get("aoml_id", "")),
        meta_file=meta_file if meta_file != "REPLACE_WITH_PATH_TO_META_FILE_OR_NULL" else None,
        logger=logger,
        force_reprocess=force_reprocess,
    )

    for p in phy_result["errors"]:
        _update_profile_status(master_status, float_id, p, "phy_parsing",
                               "failure: see log for details")

    # -------------------------------------------------------------------------
    # Step 5: NC from CSV (PROFILES → DMODE/.../FROM_PROFILE)
    # -------------------------------------------------------------------------
    nc_profile_dir = os.path.join(float_dir, "DMODE", f"{float_id}_0", f"{float_id}_0_FROM_PROFILE")
    broken_float = int(float_cfg.get("broken_float", 0))

    wmo_id = str(float_cfg.get("wmo_id", ""))

    nc_csv_result = nc_from_csv.run(
        profiles_dir=profiles_dir,
        nc_dir=nc_profile_dir,
        float_num=wmo_id,
        broken_float=broken_float,
        logger=logger,
        force_reprocess=force_reprocess,
    )

    # Update nc_parsing status for each profile using nc_csv_result directly.
    # nc_csv_result["nc_files"] contains paths like {wmo_id}-032.nc (3-digit profile nums).
    # nc_csv_result["errors"] contains 3-digit profile number strings.
    for nc_path in nc_csv_result["nc_files"]:
        # Extract profile number from filename: 2904019-032.nc → "032"
        basename = os.path.basename(nc_path)
        try:
            p = basename.split("-")[1].split(".nc")[0]
            _update_profile_status(master_status, float_id, p, "nc_parsing", "success")
        except (IndexError, ValueError):
            pass
    for p in nc_csv_result["errors"]:
        _update_profile_status(master_status, float_id, p, "nc_parsing",
                               "failure: see log for details")

    # -------------------------------------------------------------------------
    # Step 6: ARGO RT download (→ ARGO_RT_NETCDF_FILES)
    # -------------------------------------------------------------------------
    argo_nc_dir = os.path.join(float_dir, "ARGO_RT_NETCDF_FILES")
    argo_url = float_cfg.get("argo_url", "")

    if argo_url and "REPLACE_WITH_WMO_ID" not in argo_url:
        argo_dl_result = argo_downloader.run(
            url=argo_url,
            argo_nc_dir=argo_nc_dir,
            wmo_id=wmo_id,
            logger=logger,
            force_reprocess=force_reprocess,
        )
        _update_argo_rt_status(master_status, float_id, "last_download_date", run_time.isoformat())
        _update_argo_rt_status(
            master_status, float_id, "download_status",
            "success" if not argo_dl_result["errors"] else f"partial failure: {len(argo_dl_result['errors'])} file(s) failed"
        )
    else:
        logger.info("ARGO_DOWNLOAD", "No argo_url configured — skipping ARGO RT download.")
        logger.success("ARGO_DOWNLOAD")
        argo_dl_result = {"downloaded_files": [], "errors": []}

    # -------------------------------------------------------------------------
    # Step 7: NC from ARGO (ARGO_RT_NETCDF_FILES → DMODE/.../FROM_ARGO)
    # -------------------------------------------------------------------------
    nc_argo_dir = os.path.join(float_dir, "DMODE", f"{float_id}_0", f"{float_id}_0_FROM_ARGO")

    nc_argo_result = nc_from_argo.run(
        argo_nc_dir=argo_nc_dir,
        nc_dir=nc_argo_dir,
        wmo_id=wmo_id,
        logger=logger,
        force_reprocess=force_reprocess,
    )

    _update_argo_rt_status(
        master_status, float_id, "nc_parsing",
        "success" if not nc_argo_result["errors"] else f"failure: {nc_argo_result['errors']}"
    )

    # -------------------------------------------------------------------------
    # OVERALL_SUMMARY section
    # -------------------------------------------------------------------------
    all_converted = conv_result["converted_profiles"]
    all_failed_conv = conv_result["failed_profiles"]

    logger.info("OVERALL_SUMMARY", f"SBD: {sbd_result['n_downloaded']} downloaded, {sbd_result['n_skipped']} skipped, {len(sbd_result['failed_files'])} failed.")
    logger.info("OVERALL_SUMMARY", f"Profiles converted: {all_converted}")
    logger.info("OVERALL_SUMMARY", f"Profiles failed conversion: {all_failed_conv}")
    logger.info("OVERALL_SUMMARY", f"PHY files generated: {len(phy_result['phy_files'])}")
    logger.info("OVERALL_SUMMARY", f"NC (from CSV) generated: {len(nc_csv_result['nc_files'])}")
    logger.info("OVERALL_SUMMARY", f"ARGO RT downloaded: {len(argo_dl_result['downloaded_files'])}")
    logger.info("OVERALL_SUMMARY", f"NC (from ARGO) generated: {len(nc_argo_result['nc_files'])}")

    log_path = logger.write()
    print(f"  [LOG] {log_path}")

    return logger.has_errors()


# ============================================================================
# Main orchestrator
# ============================================================================

def run_all(config_path: str):
    """Run the full pipeline for all active floats in config."""
    from modules.logger import setup_console_logging
    setup_console_logging()

    config = load_config(config_path)
    output_base_dir = config["output_base_dir"]
    os.makedirs(output_base_dir, exist_ok=True)

    gmail_cfg = config.get("gmail", {})
    dmode_tools_path = config.get("dmode_tools_path", None)
    force_reprocess = bool(config.get("force_reprocess", False))
    run_time = datetime.now(timezone.utc)

    master_status = load_master_status(output_base_dir)

    # Authenticate once and share client across all floats
    gmail_client = None
    try:
        from modules.sbd_downloader import build_client
        gmail_client = build_client(gmail_cfg)
        print("[AUTH] Gmail authentication successful.")
    except Exception as e:
        print(f"[AUTH] Gmail authentication failed: {e}")
        print("[AUTH] SBD download will be skipped for all floats.")

    floats = [f for f in config.get("floats", []) if f.get("active", True)]
    print(f"[RUN] Processing {len(floats)} active float(s) at {run_time.strftime('%Y-%m-%dT%H:%M:%SZ')} UTC")

    any_errors = False
    for float_cfg in floats:
        float_id = float_cfg.get("float_id", "UNKNOWN")
        # Skip placeholder entries
        if float_cfg.get("imei", "REPLACE_WITH_IMEI") == "REPLACE_WITH_IMEI":
            print(f"  [{float_id}] IMEI not configured — skipping.")
            continue

        print(f"\n{'='*60}\n  Float: {float_id}\n{'='*60}")
        had_errors = run_float_pipeline(
            float_cfg=float_cfg,
            gmail_cfg=gmail_cfg,
            output_base_dir=output_base_dir,
            master_status=master_status,
            run_time=run_time,
            gmail_client=gmail_client,
            dmode_tools_path=dmode_tools_path,
            force_reprocess=force_reprocess,
        )
        any_errors = any_errors or had_errors

    save_master_status(output_base_dir, master_status)
    print(f"\n[DONE] master_status.json updated at {output_base_dir}")

    if any_errors:
        print("[DONE] Some floats had errors — check per-float logs for details.")
    else:
        print("[DONE] All floats completed successfully.")


# ============================================================================
# Entry point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="ARGO Float Automated Processing Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Run once:              python workflow.py
  Custom config:         python workflow.py --config my_config.json
  Scheduled:             python workflow.py --schedule
  Gmail auth setup:      python workflow.py --setup-gmail-auth
        """,
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG,
        help="Path to config.json (default: config.json in this directory).",
    )
    parser.add_argument(
        "--schedule", action="store_true",
        help="Run on an automated schedule (daily/weekly/monthly as set in config.json).",
    )
    parser.add_argument(
        "--setup-gmail-auth", action="store_true",
        help="Interactive Gmail OAuth2 setup — opens browser to generate token.json. Run once before scheduling.",
    )
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"ERROR: config.json not found at '{args.config}'.")
        print("Copy and fill in the template: config.json in the workflow directory.")
        sys.exit(1)

    config = load_config(args.config)

    # ---- Gmail auth setup ----
    if args.setup_gmail_auth:
        from modules.sbd_downloader import setup_gmail_auth
        print("Starting interactive Gmail authentication...")
        setup_gmail_auth(config["gmail"])
        return

    # ---- Scheduled mode ----
    if args.schedule:
        BlockingScheduler, CronTrigger = _import_scheduler()

        schedule_cfg = config.get("schedule", {})
        frequency = schedule_cfg.get("frequency", "daily").lower()
        time_str = schedule_cfg.get("time", "06:00")
        hour, minute = time_str.split(":")

        scheduler = BlockingScheduler(timezone="UTC")

        if frequency == "daily":
            trigger = CronTrigger(hour=int(hour), minute=int(minute))
        elif frequency == "weekly":
            trigger = CronTrigger(day_of_week="mon", hour=int(hour), minute=int(minute))
        elif frequency == "monthly":
            trigger = CronTrigger(day=1, hour=int(hour), minute=int(minute))
        else:
            print(f"ERROR: Unknown schedule frequency '{frequency}'. Use: daily, weekly, monthly.")
            sys.exit(1)

        scheduler.add_job(run_all, trigger=trigger, args=[args.config], id="workflow")
        print(f"[SCHEDULER] Scheduled {frequency} at {time_str} UTC. Press Ctrl+C to stop.")
        print(f"[SCHEDULER] Next run: {scheduler.get_jobs()[0].next_run_time}")

        try:
            # Run immediately on startup, then hand off to scheduler
            print("[SCHEDULER] Running immediately on startup...")
            run_all(args.config)
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            print("\n[SCHEDULER] Stopped by user.")
        return

    # ---- Single run ----
    run_all(args.config)


if __name__ == "__main__":
    main()
