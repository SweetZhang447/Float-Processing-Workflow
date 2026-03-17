"""
gmail_client.py — Gmail API wrapper.

Adapted from sbd_auto_download/client.py.
Key change: accepts pre-built Credentials instead of calling authenticate() internally,
so auth can be managed centrally by the workflow orchestrator.
"""

import base64
from typing import List, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from google.oauth2.credentials import Credentials


class GmailApi:
    def __init__(self, creds: Credentials):
        """
        Args:
            creds: Authenticated Google OAuth2 Credentials (from gmail_auth.authenticate()).
        """
        self.service = build("gmail", "v1", credentials=creds)

    def search_emails(self, query_str: str) -> Optional[List[dict]]:
        """
        Returns list of email message stubs matching query_str.

        Args:
            query_str: Gmail search query, e.g. 'subject:300534062573580 has:attachment after:2026/01/01'

        Returns:
            List of {id, threadId} dicts, or None if no results.
        """
        request = self.service.users().messages().list(userId="me", q=query_str)
        result = self._execute_request(request)

        all_messages = result.get("messages") or []
        next_page_token = result.get("nextPageToken")

        while next_page_token:
            paged_req = self.service.users().messages().list(
                userId="me", q=query_str, pageToken=next_page_token
            )
            paged_result = self._execute_request(paged_req)
            all_messages.extend(paged_result.get("messages") or [])
            next_page_token = paged_result.get("nextPageToken")

        return all_messages or None

    def get_message_detail(
        self,
        message_id: str,
        msg_format: str = "metadata",
        metadata_headers: Optional[List[str]] = None,
    ) -> dict:
        """
        Returns details of an email message.

        Args:
            message_id:       Gmail message ID.
            msg_format:       'full' for complete payload; 'metadata' for headers only.
            metadata_headers: List of header names to return (used with 'metadata' format).
        """
        req = self.service.users().messages().get(
            userId="me",
            id=message_id,
            format=msg_format,
            metadataHeaders=metadata_headers,
        )
        return self._execute_request(req)

    def get_attachment_info(self, message_id: str, attachment_id: str) -> dict:
        """
        Retrieves the base64-encoded binary data for a message attachment.

        Args:
            message_id:    Gmail message ID.
            attachment_id: Attachment ID from the message payload.

        Returns:
            Dict with 'data' key containing base64url-encoded attachment bytes.
        """
        req = self.service.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id
        )
        return self._execute_request(req)

    @staticmethod
    def _execute_request(request) -> dict:
        try:
            return request.execute()
        except HttpError as e:
            raise RuntimeError(f"Gmail API error: {e}") from e
