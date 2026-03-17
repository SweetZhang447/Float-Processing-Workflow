"""
gmail_auth.py — Gmail OAuth2 authentication.

Adapted from sbd_auto_download/auth.py.
Key change: accepts dynamic credentials_file and token_file paths (from config.json)
instead of hardcoded paths relative to the script location.

Headless mode (used by APScheduler scheduled tasks):
  - If token.json exists and is valid (or can be silently refreshed), proceeds normally.
  - If a browser login is required, raises RuntimeError with a clear message instead of
    hanging or trying to open a browser window.

Interactive mode (run once manually to generate token.json):
  python -c "from modules.gmail_auth import authenticate; authenticate(headless=False,
      credentials_file='path/to/credentials.json', token_file='path/to/token.json')"
"""

import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def authenticate(
    credentials_file: str,
    token_file: str,
    headless: bool = True,
) -> Credentials:
    """
    Authenticate with Gmail API and return valid Credentials.

    Args:
        credentials_file: Path to credentials.json (downloaded from Google Cloud Console).
        token_file:        Path to token.json (auto-generated on first interactive run).
        headless:          If True, raises RuntimeError instead of opening a browser when
                           interactive login is needed (safe for scheduled tasks).

    Returns:
        google.oauth2.credentials.Credentials

    Raises:
        RuntimeError:     headless=True and browser login is required.
        FileNotFoundError: credentials.json not found at the given path.
    """
    if not os.path.exists(credentials_file):
        raise FileNotFoundError(
            f"credentials.json not found at '{credentials_file}'. "
            "Download it from Google Cloud Console (APIs & Services → Credentials) "
            "and set the path in config.json under gmail.credentials_file."
        )

    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Silent refresh — works headlessly without browser
            creds.refresh(Request())
        else:
            if headless:
                raise RuntimeError(
                    "Gmail authentication required but running in headless/scheduled mode.\n"
                    "Run the following once in a terminal to generate token.json:\n\n"
                    "  python workflow.py --setup-gmail-auth\n\n"
                    "Then re-run the workflow."
                )
            # Interactive browser flow — for first-time setup only
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)

        # Persist refreshed / new credentials
        with open(token_file, "w") as fh:
            fh.write(creds.to_json())

    return creds
