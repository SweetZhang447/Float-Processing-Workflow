# ARGO Float Automated Processing Pipeline

Automated end-to-end pipeline for processing Iridium float telemetry data. Downloads raw SBD files from Gmail, converts them to oceanographic profiles, parses to `.phy` (NOAA database file format) and intermediate `.nc` format (for downstream QC), and downloads/parses real-time ARGO netCDF files — all without manual intervention.

---

## Pipeline Overview

```
Step 1  Gmail → SBD_FILES/                       (download .sbd/.sts attachments)
Step 2  SBD_FILES/ → [temp] .gz                  (assemble SBD messages into .gz)
Step 3  [temp] .gz → PROFILES/                   (.gz → .bin → .csv/.txt; intermediates deleted)
Step 4  PROFILES/ → PHY_FILES/                   (parse to NOAA .phy format)
Step 5  PROFILES/ → DMODE/.../FROM_PROFILE/      (parse to intermediate .nc)
Step 6  ARGO data center → ARGO_RT_NETCDF_FILES/ (download real-time ARGO .nc)
Step 7  ARGO_RT_NETCDF_FILES/ → DMODE/.../FROM_ARGO/  (parse ARGO RT to .nc)
Step 8  FROM_PROFILE + FROM_ARGO → DMODE/.../{float_id}_0/  (merge best-available .nc set)
```

Steps 1–5 run for each float using SBD telemetry. Steps 6–7 use the ARGO data center. Step 8 merges the two NC sets into a single best-available output. All steps are non-blocking: if one step fails, the pipeline logs the error and continues.

Lack of float-level parallelism is by design — no need, as the bottleneck only exists with 50+ floats, and current deployment will not reach that scale.

---

## Output Directory Structure

```
{output_base_dir}/
├── F9184/
│   ├── Logs/
│   │   └── F9184_20260316T060000.log      ← timestamped log per run
│   ├── F9184_master_status.csv            ← per-profile status across all runs
│   ├── SBD_FILES/                         ← original .sbd and .sts files (never modified)
│   ├── PROFILES/                          ← science_log.csv, vitals_log.csv, system_log.txt
│   │   └── .sbd_conversion_state.json     ← conversion state/profile cache
│   ├── PHY_FILES/                         ← .phy files
│   ├── ARGO_RT_NETCDF_FILES/              ← downloaded real-time ARGO .nc files
│   └── DMODE/
│       └── F9184_0/
│           ├── F9184_0_FROM_PROFILE/      ← .nc files from SBD pipeline
│           ├── F9184_0_FROM_ARGO/         ← .nc files from ARGO RT pipeline
│           └── F9184_0/                   ← merged best-available .nc set (Step 8)
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

**`{IMEI}_{float_id}_state.json`** — Gmail download cache. Stores every Gmail message ID that has already been downloaded, the date used as the `after:` bound for the next Gmail query, and the timestamp of the last run. Only emails whose ID is not in this file are downloaded — prevents re-downloading the same SBD twice.

**`.sbd_conversion_state.json`** — Conversion state cache. Tracks the list of processed SBD filenames and a record per profile:

```json
{
  "processed_sbd_files": ["300534060836190_000001.sbd", ...],
  "profiles": [
    {
      "prof_num": "001",
      "momsn_range": [1, 52],
      "num_messages": 52,
      "num_messages_received": 52,
      "status": "complete"
    },
    ...
  ]
}
```

`status` is `"complete"` if all expected SBD messages were received, or `"incomplete"` if the profile was assembled from a partial transmission. Incomplete profiles are re-evaluated after each run — if all 3 output files are present and the science CSV contains ≥ 200 `LGR_CP_PTSCI` rows, the status is automatically upgraded to `"complete"`.

Deleting either file is safe — the next run will re-query or re-convert from scratch without data loss. On first run, if existing `.sbd` files are already present in `SBD_FILES/`, the script will not re-download them — just drop files in the correct folder.

---

## Incremental SBD Processing

On each run, the converter only reprocesses SBD files that are new or needed to complete an incomplete profile — not the entire archive. The start MOMSN is determined by:

1. The minimum MOMSN among newly downloaded SBD files.
2. The start MOMSN of any incomplete profiles that come after the last complete profile (those need to be re-assembled with the new data).

The earlier of the two anchors is used. Non-numeric profile entries (e.g. `system.cfg`) are excluded from the incomplete profile scan to avoid false anchoring.

---

## Master Status CSV

`{float_id}_master_status.csv` is created on first run and updated incrementally on each subsequent run. Each row represents one profile number:

| Column | Values | Description |
|--------|--------|-------------|
| `profile_number` | `001`, `002`, … | Zero-padded 3-digit profile number |
| `status_profiles` | `COMPLETE`, `INCOMPLETE` | From `.sbd_conversion_state.json` |
| `phy_files` | `DONE`, `ERROR`, `` | PHY file generation status |
| `argo_RT_netcdf_file_download_status` | `DONE RT`, `DONE DMODE`, `ERROR`, `` | RT = real-time file; DMODE = delayed-mode file |
| `DMODE_from_argo` | `DONE`, `ERROR`, `` | NC generation from ARGO RT file |
| `DMODE_from_profile` | `DONE`, `ERROR`, `` | NC generation from SBD profile |
| `DMODE_ALL` | `PROFILE`, `RT_ARGO`, `ERROR`, `` | Source used in the merged Step 8 output |

The CSV merge is field-level: on each run, only changed fields are updated. Unchanged rows are preserved. If `force_reprocess` is set for a float, the CSV is rewritten from scratch for that float.

---

## Installation

**Python 3.12+** required.

```bash
pip install -r requirements.txt
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
| `force_reprocess` | Object config for targeted reprocessing — see below |
| `floats[].float_id` | Internal float ID (e.g. `"F9184"`) |
| `floats[].imei` | 15-digit Iridium IMEI number |
| `floats[].aoml_id` | AOML internal ID number (e.g. `"9542"`) — used in `.phy` filename prefix |
| `floats[].wmo_id` | WMO float ID — used for ARGO RT download URL and `.nc` filenames |
| `floats[].broken_float` | `0` = normal; `1` = broken float fallback (LGR_PTSCI mode) |
| `floats[].argo_url` | ARGO data center URL for real-time `.nc` files |
| `floats[].meta_file` | Full path to the float's `.meta` file (used by PHY parser); set to `null` to skip PHY parsing |
| `floats[].active` | `true` to include this float in automated runs; `false` to skip |

### force_reprocess

Regenerates all intermediate files (profile CSVs, `.phy`, `.nc`) for the listed floats, ignoring cached state.

```json
"force_reprocess": { "FLOATS": ["F9184", "F9185"] }
```

To reprocess every active float in one go:

```json
"force_reprocess": { "FLOATS": ["ALL"] }
```

To disable forced reprocessing:

```json
"force_reprocess": { "FLOATS": [] }
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

Log files older than **3 months** are automatically deleted at the end of each run.

The log is structured by pipeline section:

```
[OVERALL_SUMMARY]
  [INFO]  Pipeline started for F9184 at 2026-03-16T06:00:00Z
  [INFO]  Profiles converted: ['001', '002']

[SBD_DOWNLOAD]
  [INFO]  Gmail query: subject:300534060836190 has:attachment after:2026/03/15
  [INFO]  New emails to process: 3
  [INFO]  Summary: 12 downloaded, 0 overwritten, 0 skipped, 0 failed.
  [WARN]  Missing MOMSN 000087 - 000088

[PROFILE_CONVERSION]
  [INFO]  Incremental run: 14 SBD files from MOMSN 87.
  [INFO]  Copied 14 .sbd files to temp dir.

[PHY_PARSING]
  [INFO]  Profile 001: success → 9184_009184_001.phy

[NC_FROM_CSV]
  [INFO]  Profile 001: success → 2904019-001.nc

[ARGO_DOWNLOAD]
  [INFO]  Downloaded: 45, Failed: 0

[NC_FROM_ARGO]
  [INFO]  R2904019_001.nc: success → 2904019-001.nc

[DMODE_MERGE]
  [INFO]  Profile 001: PROFILE → 2904019-001.nc
  [INFO]  Profile 045: RT_ARGO → 2904019-045.nc
```

---

## MOMSN Gap Detection

After each download run, the downloader checks the MOMSN sequence of newly downloaded SBD files for gaps:

```
[WARN]  Missing MOMSN 000087 - 000088
[INFO]  Gaps may be files still in-transit; they will be caught on the next run if received.
```

On normal runs, only the new batch is checked. When `force_reprocess` is set, the full archive is scanned. **No automatic recovery is attempted** — manual follow-up with the data provider is required for truly lost transmissions.

---

## The `broken_float` Flag

Some floats have a hardware issue where continuous profiling (CP) mode does not record data correctly. For these floats, the pipeline falls back to individual LGR_PTSCI readings instead of LGR_CP_PTSCI bin-averaged readings.

Set `"broken_float": 1` in `config.json` for the affected float. This is applied at both the `.nc` parsing step (Step 5) and is reflected in the `.phy` parsing logic (Step 4).

---

## Module Reference

| Module | Adapted From | Description |
|--------|-------------|-------------|
| `modules/gmail_auth.py` | `sbd_auto_download/auth.py` | Gmail OAuth2 authentication |
| `modules/gmail_client.py` | `sbd_auto_download/client.py` | Gmail API wrapper |
| `modules/sbd_downloader.py` | `sbd_auto_download/dload_sbd.py` | Step 1: SBD download |
| `modules/sbd_converter.py` | `teledyne_scripts_to_functions/` | Steps 2–3: SBD → profiles |
| `modules/profile_validator.py` | *(new)* | Upgrades incomplete profiles to complete if data threshold is met |
| `modules/phy_parser.py` | `NOAA_phy_broken_profiles.py` + `NOAA_phy_functions.py` | Step 4: `.phy` generation |
| `modules/nc_from_csv.py` | `make_origin_nc_files.py::read_csv_files` | Step 5: `.nc` from profiles |
| `modules/argo_downloader.py` | `make_origin_nc_files.py::download_files` | Step 6: ARGO RT download |
| `modules/nc_from_argo.py` | `make_origin_nc_files.py::read_argo_nc_files` | Step 7: `.nc` from ARGO RT |
| `modules/dmode_merger.py` | *(new)* | Step 8: merge best-available NC set |

**Note:** `nc_from_csv.py` and `nc_from_argo.py` import `to_julian_day` and `make_nc_file_origin` directly from `dmode_tools/` at runtime via `sys.path`. The dmode_tools source files are **not copied** — they are referenced in place.

---

## Troubleshooting

**`Gmail authentication required but running in headless/scheduled mode`**
→ Run `python workflow.py --setup-gmail-auth` to generate `token.json`, then retry.

**`Could not import from dmode_tools`**
→ Add `"dmode_tools_path": "C:/path/to/dmode_tools"` to `config.json`.

**`meta_file not found`**
→ Check the `meta_file` path in `config.json` for the affected float, or set it to `null` to skip PHY parsing.

**Profile shows `ERROR` in master_status.csv**
→ Check the Logs folder for that float. For profile conversion failures, the most likely cause is an incomplete `.gz` (mid-transmission). It will be retried automatically on the next run. If the gap persists, check the MOMSN gap report in the `SBD_DOWNLOAD` log section.

**Incremental run processing far more files than expected**
→ An old incomplete profile in `.sbd_conversion_state.json` is anchoring the start MOMSN. Check the `profiles` array for an incomplete entry with a low `momsn_range[0]`. If it is permanently lost, it can be manually removed from the state file.

**`broken_float` profiles have very few data points**
→ Set `"broken_float": 1` in `config.json` for that float to switch to LGR_PTSCI fallback mode.
