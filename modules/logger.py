"""
Shared logging utilities for the ARGO float automated workflow.

Each workflow run creates a single timestamped log file per float under:
    {float_dir}/Logs/F{float_id}_{YYYYMMDDTHHMMSS}.log

Log sections:
    OVERALL_SUMMARY       - profiles operated on, high-level result
    SBD_DOWNLOAD          - Gmail download results, MOMSN gaps
    PROFILE_CONVERSION    - sbd->gz->bin->csv conversion results
    PHY_PARSING           - .phy output results
    NC_FROM_CSV           - .nc from profile CSV results
    ARGO_DOWNLOAD         - real-time ARGO netcdf download results
    NC_FROM_ARGO          - .nc from ARGO RT results
"""

import logging
import os
from datetime import datetime, timezone
from io import StringIO


SECTIONS = [
    "OVERALL_SUMMARY",
    "SBD_DOWNLOAD",
    "PROFILE_CONVERSION",
    "PHY_PARSING",
    "NC_FROM_CSV",
    "ARGO_DOWNLOAD",
    "NC_FROM_ARGO",
]


class FloatLogger:
    """
    Per-float, per-run logger. Collects section-by-section messages and writes
    a structured log file at the end of the run.
    """

    def __init__(self, float_id: str, float_dir: str, run_time: datetime | None = None):
        self.float_id = float_id
        self.float_dir = float_dir
        self.run_time = run_time or datetime.now(timezone.utc)
        self._sections: dict[str, list[str]] = {s: [] for s in SECTIONS}
        self._log_path: str | None = None

        # Also mirror to a Python logger for real-time console visibility
        self._py_logger = logging.getLogger(f"workflow.{float_id}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def info(self, section: str, message: str) -> None:
        """Record an informational message under a section."""
        self._append(section, f"[INFO]  {message}")
        self._py_logger.info("[%s] %s", section, message)

    def warning(self, section: str, message: str) -> None:
        """Record a warning under a section."""
        self._append(section, f"[WARN]  {message}")
        self._py_logger.warning("[%s] %s", section, message)

    def error(self, section: str, message: str) -> None:
        """Record an error under a section."""
        self._append(section, f"[ERROR] {message}")
        self._py_logger.error("[%s] %s", section, message)

    def file_only(self, section: str, message: str) -> None:
        """Record to log file only — not printed to console."""
        self._append(section, f"[INFO]  {message}")

    def success(self, section: str) -> None:
        """Mark a section as having no errors (called when section completes cleanly)."""
        if not self._sections[section]:
            self._append(section, "[INFO]  success")

    def write(self):
        """Flush all collected messages to the timestamped log file."""
        logs_dir = os.path.join(self.float_dir, "Logs")
        os.makedirs(logs_dir, exist_ok=True)

        ts = self.run_time.strftime("%Y%m%dT%H%M%S")
        self._log_path = os.path.join(logs_dir, f"{self.float_id}_{ts}.log")

        buf = StringIO()
        buf.write("=" * 70 + "\n")
        buf.write(f"ARGO Float Workflow Log\n")
        buf.write(f"Float:    {self.float_id}\n")
        buf.write(f"Run time: {self.run_time.strftime('%Y-%m-%dT%H:%M:%SZ')} UTC\n")
        buf.write("=" * 70 + "\n\n")

        for section in SECTIONS:
            buf.write(f"[{section}]\n")
            messages = self._sections[section]
            if not messages:
                buf.write("  (no activity)\n")
            else:
                for msg in messages:
                    buf.write(f"  {msg}\n")
            buf.write("\n")

        content = buf.getvalue()
        with open(self._log_path, "w", encoding="utf-8") as f:
            f.write(content)

        self._py_logger.info("Log written to %s", self._log_path)
        return self._log_path

    def log_path(self) -> str | None:
        return self._log_path

    def has_errors(self, section: str | None = None) -> bool:
        """Return True if any ERROR messages exist (optionally scoped to one section)."""
        sections = [section] if section else SECTIONS
        for s in sections:
            if any(m.startswith("[ERROR]") for m in self._sections.get(s, [])):
                return True
        return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _append(self, section: str, message: str) -> None:
        if section not in self._sections:
            raise ValueError(f"Unknown log section '{section}'. Valid sections: {SECTIONS}")
        self._sections[section].append(message)


def setup_console_logging(level: int = logging.INFO) -> None:
    """Configure root logger to print to console. Call once at startup."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(name)-30s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
