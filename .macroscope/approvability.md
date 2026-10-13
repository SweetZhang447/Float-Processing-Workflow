---
neverApprove:
  - "modules/sbd_downloader.py"
  - "modules/sbd_converter.py"
  - "modules/gmail_auth.py"
  - "requirements.txt"
---

Require human review for any change that alters how SBD files are downloaded, assembled,
or tracked in the state files (`{IMEI}_{float_id}_state.json`, `.sbd_conversion_state.json`),
since a mistake there can silently drop or duplicate oceanographic profiles.

Require human review for changes to output file naming, the `.phy` / `.nc` formats, or the
master status CSV columns; downstream QC and the NOAA database depend on them.

Documentation, comment, and docstring fixes, log message wording, and small self-contained
changes that don't alter pipeline output are eligible for approval.
