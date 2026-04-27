"""
argo_downloader.py — Step 6: Download real-time ARGO netCDF profile files.

Adapted from make_origin_nc_files.py::download_files() and download_file().

Key changes vs. original:
  - Fixed logical OR bug: original `if (f"R{float_num}" or f"D{float_num}") in href`
    always evaluates True (Python OR returns first truthy operand, which is a non-empty string).
    Fixed to: `if (f"R{wmo_id}" in href) or (f"D{wmo_id}" in href)`.
  - Exposes run() function returning structured result dict.
  - Integrates with FloatLogger for error logging.
  - ProcessPoolExecutor scope fixed: executor is created once outside the link loop
    (original created a new executor per link, discarding tasks immediately).
"""

import os
import requests
from concurrent.futures import ProcessPoolExecutor
from typing import TypedDict

from bs4 import BeautifulSoup

from modules.logger import FloatLogger



class ArgoDownloadResult(TypedDict):
    downloaded_files: list[str]
    new_files: list[str]
    errors: list[str]


# ============================================================================
# Internal download helper (must be top-level for ProcessPoolExecutor pickling)
# ============================================================================

def _download_file(dload_url: str, download_dir: str, filename: str) -> str:
    """
    Download a single file from dload_url and save it to download_dir/filename.
    Returns the local file path on success.
    Raises RuntimeError on HTTP error.
    """
    response = requests.get(dload_url, timeout=30)
    if response.status_code == 200:
        dest = os.path.join(download_dir, filename)
        with open(dest, "wb") as f:
            f.write(response.content)
        return dest
    else:
        raise RuntimeError(f"HTTP {response.status_code} downloading {dload_url}")


# ============================================================================
# Public API
# ============================================================================

def run(
    url: str,
    argo_nc_dir: str,
    wmo_id: str,
    logger: FloatLogger,
    force_reprocess: bool = False,
) -> ArgoDownloadResult:
    """
    Download real-time ARGO netCDF profile files for one float.

    Args:
        url:              ARGO data center URL listing float profiles
                          (e.g. 'https://data-argo.ifremer.fr/dac/aoml/1902655/profiles/').
        argo_nc_dir:      Local directory where downloaded .nc files are saved.
        wmo_id:           Float WMO ID (used to filter profile links, e.g. '1902655').
        logger:           FloatLogger instance.
        force_reprocess:  If True, re-download files even if they already exist locally.

    Returns:
        dict with keys:
            downloaded_files (list of str — local paths of all files, including pre-existing)
            new_files        (list of str — local paths of files downloaded this run only)
            errors           (list of str — URLs that failed to download)
    """
    os.makedirs(argo_nc_dir, exist_ok=True)

    logger.info("ARGO_DOWNLOAD", f"Fetching directory listing: {url}")

    try:
        res = requests.get(url, timeout=30)
        res.raise_for_status()
    except requests.RequestException as e:
        logger.error("ARGO_DOWNLOAD", f"Failed to fetch directory listing: {e}")
        return {"downloaded_files": [], "new_files": [], "errors": [str(url)]}

    soup = BeautifulSoup(res.text, "html.parser")

    # Collect links matching R{wmo_id} or D{wmo_id} (real-time and delayed mode files).
    # Bug fix: original used `(f"R{float_num}" or f"D{float_num}") in href`
    # which always evaluates True (OR returns first truthy str). Correct check below:
    download_links = []
    for link in soup.find_all('a'):
        href = link.get('href', '')
        if (f"R{wmo_id}" in href) or (f"D{wmo_id}" in href):
            filename = os.path.basename(href.rstrip('/'))
            full_url = url.rstrip('/') + '/' + href.lstrip('/')
            download_links.append((full_url, filename))

    logger.info("ARGO_DOWNLOAD", f"Profile files found: {len(download_links)}")

    if not download_links:
        logger.warning("ARGO_DOWNLOAD", "No matching profile links found. Check wmo_id and url in config.")
        return {"downloaded_files": [], "new_files": [], "errors": []}

    downloaded_files = []
    errors = []

    # Filter out files that already exist locally (unless force_reprocess)
    links_to_download = []
    for full_url, filename in download_links:
        local_path = os.path.join(argo_nc_dir, filename)
        if os.path.exists(local_path) and not force_reprocess:
            logger.file_only("ARGO_DOWNLOAD", f"Skipping existing: {filename}")
            downloaded_files.append(local_path)
        else:
            links_to_download.append((full_url, filename))

    if not links_to_download:
        logger.info("ARGO_DOWNLOAD", f"All {len(downloaded_files)} file(s) already present — nothing to download.")
        logger.success("ARGO_DOWNLOAD")
        return {"downloaded_files": downloaded_files, "new_files": [], "errors": []}

    new_files = []
    with ProcessPoolExecutor() as executor:
        futures = {
            executor.submit(_download_file, full_url, argo_nc_dir, filename): (full_url, filename)
            for full_url, filename in links_to_download
        }
        for future, (full_url, filename) in futures.items():
            try:
                local_path = future.result()
                downloaded_files.append(local_path)
                new_files.append(local_path)
            except Exception as e:
                errors.append(full_url)
                logger.error("ARGO_DOWNLOAD", f"Failed to download {filename}: {e}")

    logger.info("ARGO_DOWNLOAD", f"Downloaded: {len(new_files)}, Failed: {len(errors)}")
    if not errors:
        logger.success("ARGO_DOWNLOAD")

    return {"downloaded_files": downloaded_files, "new_files": new_files, "errors": errors}
