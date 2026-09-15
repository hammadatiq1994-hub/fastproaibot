"""
One-time Google OAuth helper.

Opens a browser, asks you to sign in with the Google account that owns the
business Calendar and Gmail, then writes credentials/token.json.

That token is reused by calendar_service.py and gmail_service.py (Calendar
read/write + Gmail send). Re-run this script if you revoke access or change
the Google Cloud OAuth client.

Prerequisites
-------------
1. Create a Google Cloud project.
2. Enable the Google Calendar API and the Gmail API.
3. Create an OAuth client ID of type "Desktop app".
4. Download the JSON and save it as credentials/credentials.json.

Usage
-----
    python generate_token.py
"""

from __future__ import annotations

import sys

from google_auth_oauthlib.flow import InstalledAppFlow

import config


def main() -> int:
    logger = config.setup_logging()
    secrets_path = config.GOOGLE_CLIENT_SECRETS
    if not secrets_path.exists():
        logger.error(
            "Missing %s. Download the OAuth Desktop-app client JSON from "
            "Google Cloud Console and save it there.",
            secrets_path,
        )
        return 1

    logger.info("Starting OAuth flow. A browser window will open ...")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(secrets_path),
        scopes=config.GOOGLE_SCOPES,
    )
    # local_server works on Windows and Linux. The user signs in once.
    creds = flow.run_local_server(port=0, prompt="consent")
    config.GOOGLE_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    logger.info("Saved token to %s", config.GOOGLE_TOKEN_FILE)
    logger.info("You can now start the API: python app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
