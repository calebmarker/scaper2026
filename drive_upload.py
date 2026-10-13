"""Minimal Google Drive uploader using OAuth (your own Google account)."""

from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# drive.file only grants access to files this app creates – nothing else in your Drive.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveUploader:
    def __init__(self, credentials_file: Path, root_folder_name: str, token_file: Path = Path("token.json")):
        self.service = build("drive", "v3", credentials=self._load_creds(credentials_file, token_file))
        self._folder_cache: dict[tuple[str, str], str] = {}
        self.root_id = self._find_or_create_folder(root_folder_name, "root")

    @staticmethod
    def _load_creds(credentials_file: Path, token_file: Path) -> Credentials:
        creds = None
        if token_file.exists():
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not credentials_file.exists():
                    raise SystemExit(
                        f"Missing {credentials_file}. See README.md -> 'Google Drive setup', "
                        "or run with --no-drive."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
                creds = flow.run_local_server(port=0)
            token_file.write_text(creds.to_json())
        return creds

    def _find_or_create_folder(self, name: str, parent_id: str) -> str:
        key = (parent_id, name)
        if key in self._folder_cache:
            return self._folder_cache[key]
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = (
            f"name = '{escaped}' and mimeType = '{FOLDER_MIME}' "
            f"and '{parent_id}' in parents and trashed = false"
        )
        found = self.service.files().list(q=query, fields="files(id)", pageSize=1).execute().get("files", [])
        if found:
            folder_id = found[0]["id"]
        else:
            meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
            folder_id = self.service.files().create(body=meta, fields="id").execute()["id"]
        self._folder_cache[key] = folder_id
        return folder_id

    def ensure_folder(self, name: str) -> str:
        return self._find_or_create_folder(name, self.root_id)

    def upload(self, path: Path, folder_id: str, mime_type: str) -> str:
        media = MediaFileUpload(str(path), mimetype=mime_type, resumable=True)
        meta = {"name": path.name, "parents": [folder_id]}
        return self.service.files().create(body=meta, media_body=media, fields="id").execute()["id"]
