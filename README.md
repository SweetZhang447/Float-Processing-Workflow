# ARGO Float Automated Processing Pipeline

Automated end-to-end pipeline for processing Iridium float telemetry data. Downloads raw SBD files from Gmail, converts them to oceanographic profiles, parses to `.phy` (NOAA database file format) and intermediate `.nc` format (for downstream QC data control), and downloads/parses real-time ARGO netCDF files — all without manual intervention.

---

## Pipeline Overview

```
Step 1  Gmail → SBD_FILES/              (download .sbd/.sts attachments)
Step 2  SBD_FILES/ → [temp] .gz         (assemble SBD messages into .gz)
Step 3  [temp] .gz → PROFILES/          (.gz → .bin → .csv/.txt; intermediates deleted)
Step 4  PROFILES/ → PHY_FILES/          (parse to NOAA .phy format)
Step 5  PROFILES/ → DMODE/.../FROM_PROFILE/   (parse to intermediate .nc)
Step 6  ARGO data center → ARGO_RT_NETCDF_FILES/   (download real-time ARGO .nc)
Step 7  ARGO_RT_NETCDF_FILES/ → DMODE/.../FROM_ARGO/  (parse to intermediate .nc)
```

Steps 1–5 run for each float using SBD telemetry. Steps 6–7 run in parallel using the ARGO data center. All steps are non-blocking: if one step fails, the pipeline logs the error and continues to the next.
Lack of float level parallelism is by design, no need as bottleneck only exists with 50+ floats, and our current lifecycle and deployment of floats will not reach 50+ active floats at a time. 

---

## Output Directory Structure

```
{output_base_dir}/
├── F9184/
│   ├── Logs/
│   │   └── F9184_20260316T060000.log   ← timestamped log per run
│   ├── F9184_master_status.csv     ← appended per-run profile status summary
│   ├── SBD_FILES/                  ← original .sbd and .sts files (never modified)
│   ├── PROFILES/                   ← science_log.csv, vitals_log.csv, system_log.txt
│   ├── PHY_FILES/                  ← .phy files
│   ├── ARGO_RT_NETCDF_FILES/       ← downloaded real-time ARGO .nc files
│   └── DMODE/
│       └── F9184_0/
│           ├── F9184_0_FROM_PROFILE/   ← .nc files from SBD pipeline
│           └── F9184_0_FROM_ARGO/      ← .nc files from ARGO RT pipeline
├── F9185/
│   └── ...
```

---

## State Files

Two JSON state files are written automatically to track pipeline progress across runs:

| File | Location | Managed by |
|------|----------|------------|
| `{IMEI}_{float_id}_state.json` | `SBD_FILES/` | `sbd_downloader` |
| `.sbd_conversion_state.json` | `PROFILES/` | `sbd_converter` |

**`{IMEI}_{float_id}_state.json`** — Gmail download cache. Stores every Gmail message ID that has already been downloaded as an `.sbd` file, the date used as the `after:` bound for the next Gmail query, and the timestamp of the last run. On each run, only emails whose ID is not already in this file are downloaded — prevents re-downloading the same SBD twice.

**`.sbd_conversion_state.json`** — Conversion skip-cache. Stores the sorted list of `.sbd` filenames that were processed on the last conversion run. If the current SBD file list matches exactly, conversion is skipped entirely (no temp directory, no parsing). Bypassed when `force_reprocess` is set for that float.

Deleting either file is safe — the next run will re-query or re-convert from scratch without data loss. On first time run, if user has existing .sbd files, script will automatically generate JSON files and will not redownload any existing files -> just drop of .sbd files in the correct folder for the float. 

---

`{float_id}_master_status.csv` is created in each float directory on first run and appended on each subsequent run.
Each run section includes:

- `new_sbd_messages`
- `profiles_numbers_decoded`
- one row per profile with step-level status columns:
  - `status_profiles`
  - `phy_files`
  - `argo_RT_netcdf_file_download_status`
  - `DMODE_from_argo`
  - `DMODE_from_profile`

Values are simple `DONE` or `ERROR` or left empty if n/a.
EX: NOAA database missing PHY files -> no "ARGO_RT_NETCDF_FILES"

---

## Installation

**Python 3.10+** required.

```bash
pip install apscheduler google-api-python-client google-auth-oauthlib \
            netCDF4 numpy gsw scipy pandas requests beautifulsoup4
```

---

## Setup

### 1. Configure `config.json`

Open `config.json` and fill in all `REPLACE_WITH_*` fields:

| Field | Description |
|-------|-------------|
| `output_base_dir` | Root directory where all float data will be saved |
| `schedule.frequency` | `"daily"`, `"weekly"`, or `"monthly"` |
| `schedule.time` | Time of day to run in 24-hour UTC format (e.g. `"06:00"`) |
| `gmail.credentials_file` | Path to `credentials.json` downloaded from Google Cloud Console |
| `gmail.token_file` | Path where `token.json` will be saved (auto-generated on first auth) |
| `gmail.grace_hours` | Hours before now to cut off downloads (default `6`; avoids mid-transmission partial profiles) |
| `force_reprocess` | Object config for targeted reprocessing. Format: `{ "FLOATS": [...] }`. All profiles are reprocessed for every float listed. Leave `FLOATS` empty (`[]`) to skip forced reprocessing. |
| `floats[].float_id` | Internal float ID (e.g. `"F9184"`) |
| `floats[].imei` | 15-digit Iridium IMEI number |
| `floats[].aoml_id` | AOML internal ID number (e.g. `"9542"`) — used in `.phy` filename prefix |
| `floats[].wmo_id` | WMO float ID — used for ARGO RT download URL and `.nc` filenames |
| `floats[].broken_float` | `0` = normal; `1` = broken float fallback (LGR_PTSCI mode for F10051/F10052-type floats) |
| `floats[].argo_url` | ARGO data center URL for real-time `.nc` files |
| `floats[].meta_file` | Full path to the float's `.meta` file (used by PHY parser); set to `null` to skip PHY parsing |
| `floats[].active` | `true` to include this float in automated runs; `false` to skip |

To add more floats, copy the float entry block and fill in the fields.

Example `force_reprocess`:

```json
"force_reprocess": {
  "FLOATS": ["F9184", "F9185"]
}
```

### 2. Gmail authentication (one-time setup)

The pipeline reads Gmail via the Gmail API using OAuth2. You need:

1. A Google Cloud project with the **Gmail API** enabled.
2. OAuth2 credentials (`credentials.json`) downloaded from **Google Cloud Console → APIs & Services → Credentials**.
3. Run the interactive auth once to generate `token.json`:

```bash
python workflow.py --setup-gmail-auth
```

A browser window will open for you to authorize access. After this, scheduled runs work headlessly using the saved token (auto-refreshed silently).

---

## Usage

### Run once

```bash
python workflow.py
```

### Run with a specific config file

```bash
python workflow.py --config path/to/config.json
```

### Run on an automated schedule

```bash
python workflow.py --schedule
```

Runs immediately on startup, then fires again at the configured `schedule.time` each `schedule.frequency`. Keep this process running (e.g. in a terminal, screen session, or system service). Press `Ctrl+C` to stop.

**Cross-platform note:** APScheduler handles scheduling natively on Windows, macOS, and Linux — no OS-level cron or Task Scheduler setup needed.

---

## Logging

Each run writes a timestamped log file per float:

```
{output_base_dir}/{float_id}/Logs/{float_id}_YYYYMMDDTHHMMSS.log
```

The log is structured by pipeline section:

```
[OVERALL_SUMMARY]
  [INFO]  Pipeline started for F9184 at 2026-03-16T06:00:00Z
  [INFO]  Profiles converted: ['001', '002']
  ...

[SBD_DOWNLOAD]
  [INFO]  Gmail query: subject:300534060836190 has:attachment after:2026/03/15
  [INFO]  New emails to process: 3
  [INFO]  Summary: 12 downloaded, 0 overwritten, 0 skipped, 0 failed.
  [WARN]  Missing MOMSN 000087 - 000088  (gap — no automated recovery needed)

[PROFILE_CONVERSION]
  [INFO]  SBD files to process: 12
  [INFO]  Created .gz: 300534060836190_001_science_log.bin.gz
  ...

[PHY_PARSING]
  [INFO]  Profile 001: success → 9184_009184_001.phy

[NC_FROM_CSV]
  [INFO]  Profile 001: success → F9184-001.nc

[ARGO_DOWNLOAD]
  [INFO]  Downloaded: 45, Failed: 0

[NC_FROM_ARGO]
  [INFO]  R1902655_001.nc: success → 1902655-001.nc
```

---

## MOMSN Gap Detection

The downloader tracks the sequence of SBD message numbers (MOMSN) in `SBD_FILES/`. After each download run, it checks for gaps in the sequence and logs them:

```
[WARN]  Missing MOMSN 000087 - 000088
[INFO]  Gaps may be files still in-transit; they will be caught on the next run if received.
```

Gaps that result in an undecodable profile will show up as `ERROR` in the per-float `{float_id}_master_status.csv` under `status_profiles`. **No automatic recovery is attempted** — manual follow-up with the data provider is required for truly lost transmissions. The log provides the MOMSN range to report.

---

## The `broken_float` Flag

Some floats (e.g. F10051, F10052) have a hardware issue where the continuous profiling (CP) mode does not record data correctly. For these floats, the pipeline falls back to individual LGR_PTSCI readings instead of LGR_CP_PTSCI bin-averaged readings.

Set `"broken_float": 1` in `config.json` for the affected float. This is applied at both the `.nc` parsing step (Step 5) and is reflected in the `.phy` parsing logic (Step 4).

The bin-averaging code for broken float data (scipy `binned_statistic`, 2 DBAR bins) is preserved as commented-out code in [modules/nc_from_csv.py](modules/nc_from_csv.py) for reference.

---

## Module Reference

| Module | Adapted From | Description |
|--------|-------------|-------------|
| `modules/gmail_auth.py` | `sbd_auto_download/auth.py` | Gmail OAuth2 authentication |
| `modules/gmail_client.py` | `sbd_auto_download/client.py` | Gmail API wrapper |
| `modules/sbd_downloader.py` | `sbd_auto_download/dload_sbd.py` | Step 1: SBD download |
| `modules/sbd_converter.py` | `teledyne_scripts_to_functions/` | Steps 2–3: SBD → profiles |
| `modules/phy_parser.py` | `NOAA_phy_broken_profiles.py` + `NOAA_phy_functions.py` | Step 4: `.phy` generation |
| `modules/nc_from_csv.py` | `make_origin_nc_files.py::read_csv_files` | Step 5: `.nc` from profiles |
| `modules/argo_downloader.py` | `make_origin_nc_files.py::download_files` | Step 6: ARGO RT download |
| `modules/nc_from_argo.py` | `make_origin_nc_files.py::read_argo_nc_files` | Step 7: `.nc` from ARGO RT |

**Note:** `nc_from_csv.py` and `nc_from_argo.py` import `to_julian_day` and `make_nc_file_origin` directly from `dmode_tools/` at runtime via `sys.path`. The dmode_tools source files are **not copied** — they are referenced in place.

---

## Troubleshooting

**`Gmail authentication required but running in headless/scheduled mode`**
→ Run `python workflow.py --setup-gmail-auth` to generate `token.json`, then retry.

**`Could not import from dmode_tools`**
→ Add `"dmode_tools_path": "C:/path/to/dmode_tools"` to `config.json`.

**`meta_file not found`**
→ Check the `meta_file` path in `config.json` for the affected float, or set it to `null` to skip PHY parsing.

**Profile shows `ERROR` in `{float_id}_master_status.csv`**
→ Check the Logs folder for that float to identify the failing step and error details. For profile conversion failures, the most likely cause is an incomplete `.gz` (mid-transmission). It will be retried automatically on the next run when more SBD files arrive. If the gap persists, check the MOMSN gap report in the SBD_DOWNLOAD section of the log.

**`broken_float` profiles have very few data points**
→ Set `"broken_float": 1` in `config.json` for that float to switch to LGR_PTSCI fallback mode.
