"""
sbd_downloader.py — Step 1: Download .sbd and .sts files from Gmail attachments.

Adapted from sbd_auto_download/dload_sbd.py.

Key changes vs. original:
  - Accepts dynamic credentials/token paths from config (no hardcoded paths).
  - Windows Task Scheduler registration removed — scheduling handled by APScheduler in workflow.py.
  - Exposes run() function returning a structured result dict for the workflow orchestrator.
  - Integrates with FloatLogger for structured log output.
  - SBD_FILES directory is the single canonical location; no subdirectory reorganization.

Original features preserved:
  - Gmail message ID deduplication via per-float state JSON file.
  - Grace period window (skip emails newer than N hours) to avoid mid-transmission downloads.
  - MOMSN gap detection and logging (no automated recovery — gaps logged for user reference only).
  - Retry logic (up to 3 attempts) on HTTP errors.
  - Headless-safe auth (raises RuntimeError cleanly instead of hanging on missing token).
  - Email timestamp vs file mtime overwrite detection.
"""

import base64
import email.utils
import json
import os
import datetime as dt
from pathlib import Path
from typing import Optional, TypedDict

import googleapiclient.errors

from modules.gmail_auth import authenticate
from modules.gmail_client import GmailApi
from modules.logger import FloatLogger



class SbdResult(TypedDict):
    n_downloaded: int
    n_overwritten: int
    n_skipped: int
    failed_files: list[str]
    gaps: list[tuple[int, int]]
    new_sbd_files: list[str]


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------

def _state_path(sbd_dir: str, imei: str, float_id: str) -> Path:
    return Path(sbd_dir) / f"{imei}_{float_id}_state.json"


def _load_state(sbd_dir: str, imei: str, float_id: str) -> dict:
    """Load per-float download state from disk. Returns a fresh state dict if not found."""
    sp = _state_path(sbd_dir, imei, float_id)
    if sp.exists():
        with open(sp, "r") as f:
            state = json.load(f)
        state["downloaded_message_ids"] = set(state.get("downloaded_message_ids", []))
        return state
    return {
        "last_query_date": None,
        "downloaded_message_ids": set(),
        "last_run_utc": None,
    }


def _save_state(sbd_dir: str, imei: str, float_id: str, state: dict) -> None:
    """Persist per-float download state to disk."""
    sp = _state_path(sbd_dir, imei, float_id)
    to_save = dict(state)
    to_save["downloaded_message_ids"] = sorted(state["downloaded_message_ids"])
    with open(sp, "w") as f:
        json.dump(to_save, f, indent=2)


# ---------------------------------------------------------------------------
# Gmail query builder
# ---------------------------------------------------------------------------

def _build_query(imei: str, last_query_date: Optional[str], grace_hours: int) -> str:
    """
    Build Gmail search query with date bounds.

    - 'after' bound: last_query_date minus 1 day (buffer for clock skew / timezone edges).
    - 'before' bound: now minus grace_hours (avoids grabbing emails from active transmissions).
    """
    grace_cutoff = dt.datetime.utcnow() - dt.timedelta(hours=grace_hours)
    before_str = grace_cutoff.strftime("%Y/%m/%d")

    if last_query_date:
        after_dt = dt.datetime.strptime(last_query_date, "%Y/%m/%d") - dt.timedelta(days=1)
        after_str = after_dt.strftime("%Y/%m/%d")
        return f"subject:{imei} has:attachment after:{after_str} before:{before_str}"
    else:
        # First run — download all historical emails; grace period applies on subsequent runs
        return f"subject:{imei} has:attachment"


# ---------------------------------------------------------------------------
# MOMSN gap detection
# ---------------------------------------------------------------------------

def _get_momsn_gaps(
    sbd_dir: str,
    imei: str,
    momsns: Optional[list[int]] = None,
) -> list[tuple[int, int]]:
    """
    Return (gap_start, gap_end) tuples for missing MOMSN ranges.

    If momsns is provided, use those values directly (scoped check).
    Otherwise scan sbd_dir for all {imei}_*.sbd files (full check).

    Example: [441, 442, 444] → [(443, 443)]
    """
    if momsns is None:
        sbd_files = list(Path(sbd_dir).glob(f"{imei}_*.sbd"))
        momsns = []
        for f in sbd_files:
            try:
                momsns.append(int(f.stem.split("_")[1]))
            except (IndexError, ValueError):
                continue

    if len(momsns) < 2:
        return []

    momsns_sorted = sorted(momsns)
    gaps = []
    for i in range(len(momsns_sorted) - 1):
        if momsns_sorted[i + 1] - momsns_sorted[i] > 1:
            gaps.append((momsns_sorted[i] + 1, momsns_sorted[i + 1] - 1))
    return gaps


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _get_email_date(payload: dict) -> Optional[dt.datetime]:
    """Extract RFC 2822 Date header and return a UTC-aware datetime, or None."""
    for h in payload.get("headers", []):
        if h["name"] == "Date":
            try:
                parsed = email.utils.parsedate_to_datetime(h["value"])
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                return parsed.astimezone(dt.timezone.utc)
            except Exception:
                return None
    return None


def _email_is_newer_than_file(file_path: str, email_date: Optional[dt.datetime]) -> bool:
    """Return True if the email's send date is strictly newer than the file's mtime."""
    if email_date is None or not os.path.exists(file_path):
        return False
    file_mtime = dt.datetime.fromtimestamp(os.path.getmtime(file_path), tz=dt.timezone.utc)
    return email_date > file_mtime


# ---------------------------------------------------------------------------
# Attachment download helpers
# ---------------------------------------------------------------------------

def _build_sts_body(payload: dict, message_detail: dict, file_name: str) -> str:
    """Build .sts metadata sidecar content from Gmail message headers + decoded body."""
    parts = payload.get("parts")[0]
    raw = parts["body"]["data"].replace("-", "+").replace("_", "/")
    decoded = base64.b64decode(raw).decode("ascii")

    lines = [f"UID: {message_detail['id']}"]
    for key in ("Date", "From", "To"):
        for h in payload["headers"]:
            if h["name"] == key:
                lines.append(f"{key}: {h['value']}")
    lines.append(f"IMEI: {file_name.split('_')[0]}")
    lines.append(decoded)

    return "\n".join(lines).replace("\r", "").replace("\n\n", "\n")


def _download_sbd_attachment(
    msg_payload: dict,
    client: GmailApi,
    email_message: dict,
    sbd_dir: str,
    file_name: str,
    email_date: Optional[dt.datetime] = None,
    max_retries: int = 3,
) -> str:
    """
    Download a single .sbd binary attachment.

    Returns:
      "downloaded"  — new file written
      "overwritten" — existing file replaced by a newer Gmail version
      "skipped"     — file already up-to-date on disk
      "failed"      — all retries exhausted
    """
    body = msg_payload["body"]
    if "attachmentId" not in body:
        return "skipped"

    dest_path = os.path.join(sbd_dir, file_name)
    if os.path.exists(dest_path):
        if _email_is_newer_than_file(dest_path, email_date):
            overwrite = True
        else:
            return "skipped"
    else:
        overwrite = False

    for attempt in range(1, max_retries + 1):
        try:
            attachment = client.get_attachment_info(email_message["id"], body["attachmentId"])
            file_data = base64.urlsafe_b64decode(attachment.get("data").encode("UTF-8"))
            with open(dest_path, "wb") as fh:
                fh.write(file_data)
            return "overwritten" if overwrite else "downloaded"
        except (googleapiclient.errors.HttpError, RuntimeError) as e:
            if attempt == max_retries:
                break

    return "failed"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(
    float_cfg: dict,
    gmail_cfg: dict,
    float_dir: str,
    logger: FloatLogger,
    client: Optional[GmailApi] = None,
    force_reprocess: bool = False,
) -> SbdResult:
    """
    Download SBD/STS files for one float from Gmail.

    Args:
        float_cfg:  Float config dict from config.json (float_id, imei, ...).
        gmail_cfg:  Gmail config dict from config.json (credentials_file, token_file, grace_hours).
        float_dir:  Base output directory for this float (e.g. {output_base_dir}/F9184).
        logger:     FloatLogger instance for this run.
        client:     Optional pre-built GmailApi (pass from orchestrator to reuse auth session).

    Returns:
        dict with keys:
            n_downloaded  (int)
            n_overwritten (int)
            n_skipped     (int)
            failed_files  (list of str)
            gaps          (list of (int, int) tuples — missing MOMSN ranges)
            new_sbd_files (list of str — filenames of newly downloaded .sbd files)
    """
    float_id = float_cfg["float_id"]
    imei = float_cfg["imei"]
    grace_hours = float_cfg.get("grace_hours", gmail_cfg.get("grace_hours", 6))

    sbd_dir = os.path.join(float_dir, "SBD_FILES")
    os.makedirs(sbd_dir, exist_ok=True)

    # --- Authenticate ---
    if client is None:
        try:
            creds = authenticate(
                credentials_file=gmail_cfg["credentials_file"],
                token_file=gmail_cfg["token_file"],
                headless=True,
            )
            client = GmailApi(creds)
        except (RuntimeError, FileNotFoundError) as e:
            logger.error("SBD_DOWNLOAD", f"Authentication failed: {e}")
            return _empty_result()

    # --- Load state ---
    state = _load_state(sbd_dir, imei, float_id)

    # --- Build and run Gmail query ---
    query = _build_query(imei, state["last_query_date"], grace_hours)
    logger.info("SBD_DOWNLOAD", f"Gmail query: {query}")

    try:
        all_emails = client.search_emails(query_str=query) or []
    except RuntimeError as e:
        logger.error("SBD_DOWNLOAD", f"Gmail search failed: {e}")
        return _empty_result()

    logger.info("SBD_DOWNLOAD", f"Emails matched: {len(all_emails)}")

    new_emails = [m for m in all_emails if m["id"] not in state["downloaded_message_ids"]]
    logger.info("SBD_DOWNLOAD", f"New emails to process: {len(new_emails)}")

    if not new_emails:
        logger.success("SBD_DOWNLOAD")
        _finalize_state(state, sbd_dir, imei, float_id, grace_hours)
        return _empty_result()

    # --- Download loop ---
    n_downloaded = 0
    n_overwritten = 0
    n_skipped = 0
    failed_files = []
    new_sbd_files = []

    for email_message in new_emails:
        email_ok = True
        try:
            detail = client.get_message_detail(
                email_message["id"], msg_format="full", metadata_headers=["parts"]
            )
            payload = detail.get("payload", {})

            if "parts" not in payload:
                state["downloaded_message_ids"].add(email_message["id"])
                continue

            email_date = _get_email_date(payload)

            for part in payload["parts"]:
                file_name = part.get("filename", "")
                if not file_name:
                    continue

                # --- .sts sidecar ---
                sts_name = file_name.rsplit(".", 1)[0] + ".sts"
                sts_path = os.path.join(sbd_dir, sts_name)
                if not os.path.exists(sts_path):
                    try:
                        sts_body = _build_sts_body(payload, detail, file_name)
                        with open(sts_path, "wt") as fh:
                            fh.write(sts_body)
                        n_downloaded += 1
                    except Exception as e:
                        logger.warning("SBD_DOWNLOAD", f"Could not write {sts_name}: {e}")
                elif _email_is_newer_than_file(sts_path, email_date):
                    try:
                        sts_body = _build_sts_body(payload, detail, file_name)
                        with open(sts_path, "wt") as fh:
                            fh.write(sts_body)
                        n_overwritten += 1
                    except Exception as e:
                        logger.warning("SBD_DOWNLOAD", f"Could not overwrite {sts_name}: {e}")
                else:
                    n_skipped += 1

                # --- .sbd binary attachment ---
                status = _download_sbd_attachment(
                    part, client, email_message, sbd_dir, file_name, email_date=email_date
                )
                if status == "downloaded":
                    n_downloaded += 1
                    new_sbd_files.append(file_name)
                elif status == "overwritten":
                    n_overwritten += 1
                    new_sbd_files.append(file_name)
                elif status == "skipped":
                    n_skipped += 1
                else:
                    email_ok = False
                    failed_files.append(file_name)
                    logger.error("SBD_DOWNLOAD", f"Failed to download {file_name} after retries")

        except Exception as e:
            email_ok = False
            logger.error("SBD_DOWNLOAD", f"Error processing message {email_message['id']}: {e}")

        if email_ok:
            state["downloaded_message_ids"].add(email_message["id"])

    # --- MOMSN gap report ---
    if force_reprocess:
        gaps = _get_momsn_gaps(sbd_dir, imei)
    else:
        new_momsns = []
        for fname in new_sbd_files:
            try:
                new_momsns.append(int(Path(fname).stem.split("_")[1]))
            except (IndexError, ValueError):
                continue
        gaps = _get_momsn_gaps(sbd_dir, imei, momsns=new_momsns)
    if gaps:
        logger.warning("SBD_DOWNLOAD", f"{len(gaps)} MOMSN gap(s) detected (no automated recovery — manual follow-up may be needed):")
        for g_start, g_end in gaps:
            logger.warning("SBD_DOWNLOAD", f"  Missing MOMSN {g_start:06d} - {g_end:06d}")
        logger.info("SBD_DOWNLOAD", "Gaps may be files still in-transit; they will be caught on the next run if received.")
    else:
        logger.info("SBD_DOWNLOAD", "No MOMSN gaps detected.")

    # --- Summary ---
    logger.info(
        "SBD_DOWNLOAD",
        f"Summary: {n_downloaded} downloaded, {n_overwritten} overwritten, "
        f"{n_skipped} skipped, {len(failed_files)} failed.",
    )
    if not failed_files:
        logger.success("SBD_DOWNLOAD")

    # Clean up any spurious empty .sts artifact
    empty_sts = os.path.join(sbd_dir, ".sts")
    if os.path.exists(empty_sts):
        os.remove(empty_sts)

    _finalize_state(state, sbd_dir, imei, float_id, grace_hours)

    return {
        "n_downloaded": n_downloaded,
        "n_overwritten": n_overwritten,
        "n_skipped": n_skipped,
        "failed_files": failed_files,
        "gaps": gaps,
        "new_sbd_files": new_sbd_files,
    }


def build_client(gmail_cfg: dict) -> GmailApi:
    """
    Authenticate and return a shared GmailApi client.
    Call once at startup and pass to run() for each float to avoid re-authenticating.
    Raises RuntimeError or FileNotFoundError if auth fails.
    """
    creds = authenticate(
        credentials_file=gmail_cfg["credentials_file"],
        token_file=gmail_cfg["token_file"],
        headless=True,
    )
    return GmailApi(creds)


def setup_gmail_auth(gmail_cfg: dict):
    """
    Interactive Gmail authentication (opens browser). Call once manually to generate token.json.
    Invoked by: python workflow.py --setup-gmail-auth
    """
    creds = authenticate(
        credentials_file=gmail_cfg["credentials_file"],
        token_file=gmail_cfg["token_file"],
        headless=False,
    )
    print(f"Authentication successful. token.json saved to: {gmail_cfg['token_file']}")
    return creds


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _finalize_state(state: dict, sbd_dir: str, imei: str, float_id: str, grace_hours: int) -> None:

    grace_cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=grace_hours)
    state["last_query_date"] = grace_cutoff.strftime("%Y/%m/%d")
    state["last_run_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _save_state(sbd_dir, imei, float_id, state)


def _empty_result() -> SbdResult:
    return {
        "n_downloaded": 0,
        "n_overwritten": 0,
        "n_skipped": 0,
        "failed_files": [],
        "gaps": [],
        "new_sbd_files": [],
    }
