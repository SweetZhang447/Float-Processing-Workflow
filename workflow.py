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
Per-float status tracked in {output_base_dir}/{float_id}/{float_id}_master_status.csv.
Per-run timestamped log written to {output_base_dir}/{float_id}/Logs/.
"""
"""
SWEET ZHANG TODO:
- THOUGHTS: Broad exception catching — 16+ except Exception as e: blocks. 
---> Catching specific exceptions (IOError, ValueError, struct.error): distinguish expected vs. unexpected failures.
- datetime.utcnow() deprecation — (deprecated in Python 3.12).
---> Standardize on datetime.now(timezone.utc)
- Add a startup dependency check so missing packages (e.g., netCDF4, gsw) fail fast with a clear message rather than mid-pipeline
- Log skipped/dropped records during CSV parsing (currently silent)
- Add a --dry-run flag for previewing what would be processed
- Unbounded log accumulation
Log files in {float_dir}/Logs/ are never rotated or cleaned. Over years of daily runs, this directory will grow without limit.
--> Delete every X days
- No health check or alerting
There is no outbound notification (email, webhook, etc.) when a run completes with errors. The only output is a log file that must be manually inspected.

"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ============================================================================
# Lazy imports (deferred so --setup-gmail-auth doesn't need APScheduler etc.)
# ============================================================================

def _import_scheduler():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        return BackgroundScheduler, CronTrigger
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


def _strip_comments(obj: Any) -> Any:
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
# Force reprocess config parser
# ============================================================================

def _parse_force_reprocess_config(force_reprocess_cfg: object) -> set[str]:
    """
    Parse config:
      "force_reprocess": { "FLOATS": [...] }

    Returns a set of float IDs to reprocess (all profiles).
    """
    if force_reprocess_cfg is None:
        return set()

    if isinstance(force_reprocess_cfg, bool):
        raise ValueError(
            "Boolean 'force_reprocess' is no longer supported. "
            "Use an object: {\"FLOATS\": [..]}."
        )

    if not isinstance(force_reprocess_cfg, dict):
        raise ValueError("force_reprocess must be an object with a 'FLOATS' list.")

    floats = force_reprocess_cfg.get("FLOATS", [])
    if not isinstance(floats, list):
        raise ValueError("'FLOATS' must be a list in force_reprocess config.")

    result: set[str] = set()
    for float_id in floats:
        if not isinstance(float_id, str) or not float_id.strip():
            raise ValueError(f"Invalid float_id entry in force_reprocess.FLOATS: {float_id!r}")
        result.add(float_id)

    return result


# ============================================================================
# Per-float status CSV helpers
# ============================================================================

_PROFILE_PATTERNS = [
    re.compile(r"-(\d{3})\.nc$", re.IGNORECASE),
    re.compile(r"_(\d{3})\.nc$", re.IGNORECASE),
    re.compile(r"_(\d{3})\.phy$", re.IGNORECASE),
]


def _norm_profile_num(raw: str) -> Optional[str]:
    s = str(raw).strip()
    if not s:
        return None
    if s.isdigit():
        return f"{int(s):03d}"
    return None


def _profile_from_converter_tag(tag: str) -> Optional[str]:
    s = str(tag)
    if len(s) >= 12 and s[9:12].isdigit():
        return s[9:12]
    return _norm_profile_num(s)


def _profile_from_path(path: str) -> Optional[str]:
    name = os.path.basename(str(path))
    for pattern in _PROFILE_PATTERNS:
        match = pattern.search(name)
        if match:
            return match.group(1)
    return None


def _build_float_status_rows(
    conv_result: dict,
    phy_result: dict,
    nc_csv_result: dict,
    argo_dl_result: dict,
    nc_argo_result: dict,
) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}

    def ensure_row(profile_num: str):
        rows.setdefault(profile_num, {
            "status_profiles": "",
            "phy_files": "",
            "argo_rt_netcdf_download": "",
            "dmode_from_argo": "",
            "dmode_from_profile": "",
        })

    for tag in conv_result.get("converted_profiles", []):
        p = _profile_from_converter_tag(tag)
        if p:
            ensure_row(p)
            rows[p]["status_profiles"] = "DONE"
    for tag in conv_result.get("failed_profiles", []):
        p = _profile_from_converter_tag(tag)
        if p:
            ensure_row(p)
            rows[p]["status_profiles"] = "ERROR"

    for phy_path in phy_result.get("phy_files", []):
        p = _profile_from_path(phy_path)
        if p:
            ensure_row(p)
            rows[p]["phy_files"] = "DONE"
    for p in phy_result.get("errors", []):
        p_norm = _norm_profile_num(p)
        if p_norm:
            ensure_row(p_norm)
            rows[p_norm]["phy_files"] = "ERROR"

    for nc_path in nc_csv_result.get("nc_files", []):
        p = _profile_from_path(nc_path)
        if p:
            ensure_row(p)
            rows[p]["dmode_from_profile"] = "DONE"
    for p in nc_csv_result.get("errors", []):
        p_norm = _norm_profile_num(p)
        if p_norm:
            ensure_row(p_norm)
            rows[p_norm]["dmode_from_profile"] = "ERROR"

    for pth in argo_dl_result.get("downloaded_files", []):
        p = _profile_from_path(pth)
        if p:
            ensure_row(p)
            rows[p]["argo_rt_netcdf_download"] = "DONE"
    for pth in argo_dl_result.get("errors", []):
        p = _profile_from_path(pth)
        if p:
            ensure_row(p)
            rows[p]["argo_rt_netcdf_download"] = "ERROR"

    for nc_path in nc_argo_result.get("nc_files", []):
        p = _profile_from_path(nc_path)
        if p:
            ensure_row(p)
            rows[p]["dmode_from_argo"] = "DONE"
    for pth in nc_argo_result.get("errors", []):
        p = _profile_from_path(pth)
        if p:
            ensure_row(p)
            rows[p]["dmode_from_argo"] = "ERROR"

    return rows


_STATUS_HEADER = [
    "profile_number",
    "status_profiles",
    "phy_files",
    "argo_RT_netcdf_file_download_status",
    "DMODE_from_argo",
    "DMODE_from_profile",
]


def _read_status_csv(path: str) -> dict[str, dict[str, str]]:
    """
    Read an existing master_status.csv and return profile rows keyed by
    zero-padded 3-digit profile number (e.g. '007').
    Skips header and any non-profile rows gracefully.
    """
    rows: dict[str, dict[str, str]] = {}
    if not os.path.exists(path):
        return rows
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or not row[0].strip().isdigit():
                continue
            p = f"{int(row[0]):03d}"
            rows[p] = {
                "status_profiles":        row[1] if len(row) > 1 else "",
                "phy_files":              row[2] if len(row) > 2 else "",
                "argo_rt_netcdf_download": row[3] if len(row) > 3 else "",
                "dmode_from_argo":        row[4] if len(row) > 4 else "",
                "dmode_from_profile":     row[5] if len(row) > 5 else "",
            }
    return rows


def _update_float_status_csv(
    float_dir: str,
    float_id: str,
    conv_result: dict,
    phy_result: dict,
    nc_csv_result: dict,
    argo_dl_result: dict,
    nc_argo_result: dict,
    force_reprocess: bool = False,
) -> None:
    """
    Merge this run's profile results into the float's master_status.csv.

    Reads the existing file (if any), merges in new/updated rows keyed by
    profile number, then rewrites the complete sorted table. New data for a
    profile always overwrites the previously recorded status for that profile.
    """
    new_rows = _build_float_status_rows(
        conv_result, phy_result, nc_csv_result, argo_dl_result, nc_argo_result
    )

    if not new_rows and not force_reprocess:
        return  # nothing processed this run — leave file untouched

    out_path = os.path.join(float_dir, f"{float_id}_master_status.csv")
    existing = _read_status_csv(out_path)

    # Merge: existing rows as base, this run's rows take precedence
    merged = {**existing, **new_rows}

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(_STATUS_HEADER)
        for p in sorted(merged.keys()):
            row = merged[p]
            writer.writerow([
                p,
                row.get("status_profiles", ""),
                row.get("phy_files", ""),
                row.get("argo_rt_netcdf_download", ""),
                row.get("dmode_from_argo", ""),
                row.get("dmode_from_profile", ""),
            ])


# ============================================================================
# Log cleanup
# ============================================================================

def _cleanup_old_logs(logs_dir: str, float_id: str, max_age_days: int = 90) -> None:
    """Delete .log files in logs_dir older than max_age_days."""
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
    try:
        for entry in os.scandir(logs_dir):
            if entry.is_file() and entry.name.endswith(".log"):
                if entry.stat().st_mtime < cutoff:
                    try:
                        os.remove(entry.path)
                    except OSError:
                        pass
    except OSError:
        pass


# ============================================================================
# Directory setup
# ============================================================================

def _ensure_float_dirs(float_dir: str, float_id: str) -> None:
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
    run_time: datetime,
    gmail_client=None,
    force_reprocess: bool = False,
) -> bool:
    """
    Run the full 7-step pipeline for one float.

    Args:
        float_cfg:           Float config dict (from config.json floats[]).
        gmail_cfg:           Gmail config dict (from config.json gmail).
        output_base_dir:     Root output directory.
        run_time:            UTC datetime of this run (used for log filename).
        gmail_client:        Optional pre-built GmailApi (shared auth across floats).
        force_reprocess:     If True, regenerate all intermediate files even if they exist.
    """
    from modules.logger import FloatLogger
    from modules import sbd_downloader, sbd_converter, phy_parser
    from modules import nc_from_csv, argo_downloader, nc_from_argo

    float_id = float_cfg["float_id"]
    float_dir = os.path.join(output_base_dir, float_id)
    _ensure_float_dirs(float_dir, float_id)

    logger = FloatLogger(float_id=float_id, float_dir=float_dir, run_time=run_time)
    logger.info("OVERALL_SUMMARY", f"Pipeline started for {float_id} at {run_time.isoformat()}Z")

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

    # -------------------------------------------------------------------------
    # Step 2 & 3: SBD → Profiles (SBD_FILES → PROFILES; .gz and .bin deleted)
    # -------------------------------------------------------------------------
    sbd_dir = os.path.join(float_dir, "SBD_FILES")
    profiles_dir = os.path.join(float_dir, "PROFILES")

    _no_new_sbds = (sbd_result["n_downloaded"] == 0 and sbd_result["n_overwritten"] == 0)
    if _no_new_sbds and not force_reprocess:
        logger.info("PROFILE_CONVERSION", "No new SBD files downloaded — skipping profile conversion.")
        logger.success("PROFILE_CONVERSION")
        conv_result = {"converted_profiles": [], "failed_profiles": [], "new_profile_files": []}
    else:
        conv_result = sbd_converter.run(
            sbd_dir=sbd_dir,
            profiles_dir=profiles_dir,
            float_id=float_id,
            logger=logger,
            force_reprocess=force_reprocess,
        )

    # -------------------------------------------------------------------------
    # Step 4: PHY parsing (PROFILES → PHY_FILES)
    # -------------------------------------------------------------------------
    phy_dir = os.path.join(float_dir, "PHY_FILES")
    meta_file = float_cfg.get("meta_file") or ""

    if _no_new_sbds and not force_reprocess:
        logger.info("PHY_PARSING", "No new SBD files — skipping PHY parsing.")
        logger.success("PHY_PARSING")
        phy_result = {"phy_files": [], "errors": []}
    else:
        phy_result = phy_parser.run(
            profiles_dir=profiles_dir,
            phy_dir=phy_dir,
            float_id=float_id,
            aoml_id=str(float_cfg.get("aoml_id", "")),
            meta_file=meta_file if meta_file != "REPLACE_WITH_PATH_TO_META_FILE_OR_NULL" else None,
            logger=logger,
            force_reprocess=force_reprocess,
        )

    # -------------------------------------------------------------------------
    # Step 5: NC from CSV (PROFILES → DMODE/.../FROM_PROFILE)
    # -------------------------------------------------------------------------
    nc_profile_dir = os.path.join(float_dir, "DMODE", f"{float_id}_0", f"{float_id}_0_FROM_PROFILE")
    broken_float = int(float_cfg.get("broken_float", 0))

    wmo_id = str(float_cfg.get("wmo_id", ""))

    if _no_new_sbds and not force_reprocess:
        logger.info("NC_FROM_CSV", "No new SBD files — skipping NC from CSV.")
        logger.success("NC_FROM_CSV")
        nc_csv_result = {"nc_files": [], "errors": []}
    else:
        nc_csv_result = nc_from_csv.run(
            profiles_dir=profiles_dir,
            nc_dir=nc_profile_dir,
            float_num=wmo_id,
            broken_float=broken_float,
            logger=logger,
            force_reprocess=force_reprocess,
        )

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
    _update_float_status_csv(
        float_dir=float_dir,
        float_id=float_id,
        conv_result=conv_result,
        phy_result=phy_result,
        nc_csv_result=nc_csv_result,
        argo_dl_result=argo_dl_result,
        nc_argo_result=nc_argo_result,
        force_reprocess=force_reprocess,
    )

    log_path = logger.write()
    print(f"  [LOG] {log_path}")

    _cleanup_old_logs(os.path.join(float_dir, "Logs"), float_id)

    return logger.has_errors()


# ============================================================================
# Main orchestrator
# ============================================================================

def run_all(config_path: str) -> None:
    """Run the full pipeline for all active floats in config."""
    from modules.logger import setup_console_logging
    setup_console_logging()

    config = load_config(config_path)
    output_base_dir = config["output_base_dir"]
    os.makedirs(output_base_dir, exist_ok=True)

    gmail_cfg = config.get("gmail", {})
    # Config-driven per-float reprocess set: force_reprocess.FLOATS
    try:
        force_reprocess_by_float = _parse_force_reprocess_config(config.get("force_reprocess"))
    except ValueError as e:
        print(f"ERROR: Invalid force_reprocess config: {e}")
        sys.exit(1)
    run_time = datetime.now(timezone.utc)

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
        force_reprocess = float_id in force_reprocess_by_float
        had_errors = run_float_pipeline(
            float_cfg=float_cfg,
            gmail_cfg=gmail_cfg,
            output_base_dir=output_base_dir,
            run_time=run_time,
            gmail_client=gmail_client,
            force_reprocess=force_reprocess,
        )
        any_errors = any_errors or had_errors

    print(f"\n[DONE] Per-float status CSV files updated at {output_base_dir}")

    if any_errors:
        print("[DONE] Some floats had errors — check per-float logs for details.")
    else:
        print("[DONE] All floats completed successfully.")


# ============================================================================
# Entry point
# ============================================================================

def main() -> None:
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
            trigger = CronTrigger(hour=int(hour), minute=int(minute), timezone="UTC")
        elif frequency == "weekly":
            trigger = CronTrigger(day_of_week="mon", hour=int(hour), minute=int(minute), timezone="UTC")
        elif frequency == "monthly":
            trigger = CronTrigger(day=1, hour=int(hour), minute=int(minute), timezone="UTC")
        else:
            print(f"ERROR: Unknown schedule frequency '{frequency}'. Use: daily, weekly, monthly.")
            sys.exit(1)

        # misfire_grace_time=3600: job still runs if the scheduler wakes up to 1 hour late
        # (prevents "missed" on Windows thread scheduling delays or brief system hiccups)
        scheduler.add_job(run_all, trigger=trigger, args=[args.config], id="workflow",
                          misfire_grace_time=3600)
        print(f"[SCHEDULER] Scheduled {frequency} at {time_str} UTC. Press Ctrl+C to stop.")
        next_run = getattr(scheduler.get_jobs()[0], 'next_run_time', '(will be calculated on start)')
        print(f"[SCHEDULER] Next run: {next_run}")

        try:
            # Run immediately on startup, then hand off to scheduler
            print("[SCHEDULER] Running immediately on startup...")
            run_all(args.config)
            scheduler.start()
            print("[SCHEDULER] Running. Press Ctrl+C to stop.")
            while True:
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            scheduler.shutdown()
            print("\n[SCHEDULER] Stopped by user.")
        return

    # ---- Single run ----
    run_all(args.config)


if __name__ == "__main__":
    main()
