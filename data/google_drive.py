"""
VIX Trader BOT — Google Drive uploads (SRS v1.4 §10.1, Impl Plan §2B).

Daily batch only, never the 15-min intraday loop. Service-account auth,
drive.file scope only (files this bot creates — never full-drive).
Every call here must fail soft: a Drive error is logged + iMessaged but
must never block flatten, paper writes, or any other part of the trading
pipeline (SRS §10.1 hard rule).
"""
from __future__ import annotations

import json
from pathlib import Path

from config import (
    ENABLE_GDRIVE_UPLOAD, GDRIVE_SERVICE_ACCOUNT_JSON,
    GDRIVE_FOLDER_DOCS_ID, GDRIVE_FOLDER_DAILY_ID, GDRIVE_FOLDER_LESSONS_ID,
    GDRIVE_CONVERT_HTML_TO_GDOC,
)

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# Never upload these, even if ENABLE_GDRIVE_UPLOAD=true (SRS §10.1 hard rule).
_FORBIDDEN_NAME_FRAGMENTS = (".env", "robinhood.pickle", "UW_API_KEY", "RH_PASSWORD")


class GoogleDriveError(Exception):
    pass


def _get_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_path = Path(GDRIVE_SERVICE_ACCOUNT_JSON)
    if not sa_path.exists():
        raise GoogleDriveError(f"service account JSON not found at {sa_path}")

    creds = service_account.Credentials.from_service_account_file(str(sa_path), scopes=_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _assert_safe_filename(name: str) -> None:
    lowered = name.lower()
    for fragment in _FORBIDDEN_NAME_FRAGMENTS:
        if fragment.lower() in lowered:
            raise GoogleDriveError(f"refusing to upload a file whose name contains {fragment!r}: {name}")


def _find_existing(service, folder_id: str, name: str) -> str | None:
    query = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
    resp = service.files().list(q=query, fields="files(id, name)").execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def upload_or_replace(local_path: str, folder_id: str, name: str, convert_to_gdoc: bool = False) -> str:
    """Update-in-place when a file with this name already exists in the
    folder (SRS §10.1: 're-upload replaces yesterday's copy'). Returns the
    Drive file's webViewLink."""
    _assert_safe_filename(name)
    if not folder_id:
        raise GoogleDriveError("no folder_id configured")

    from googleapiclient.http import MediaFileUpload

    service = _get_service()
    mimetype = "text/html"
    media = MediaFileUpload(local_path, mimetype=mimetype, resumable=False)

    body = {"name": name, "parents": [folder_id]}
    if convert_to_gdoc:
        body["mimeType"] = "application/vnd.google-apps.document"

    existing_id = _find_existing(service, folder_id, name)
    if existing_id:
        update_body = {"name": name}
        if convert_to_gdoc:
            update_body["mimeType"] = "application/vnd.google-apps.document"
        result = service.files().update(
            fileId=existing_id, media_body=media, body=update_body, fields="id, webViewLink"
        ).execute()
    else:
        result = service.files().create(
            body=body, media_body=media, fields="id, webViewLink"
        ).execute()

    return result.get("webViewLink", "")


def upload_docs_bundle(requirements_html: str, plan_html: str) -> list[str]:
    """S0 / manual: push the two SRS/plan HTML files into docs/ (Impl Plan §2B)."""
    if not ENABLE_GDRIVE_UPLOAD:
        print("[google_drive] ENABLE_GDRIVE_UPLOAD=false — skipping docs upload")
        return []
    urls = []
    try:
        urls.append(upload_or_replace(
            requirements_html, GDRIVE_FOLDER_DOCS_ID, "VIX_Trader_BOT_Requirements.html",
            convert_to_gdoc=GDRIVE_CONVERT_HTML_TO_GDOC,
        ))
        urls.append(upload_or_replace(
            plan_html, GDRIVE_FOLDER_DOCS_ID, "VIX_Trader_BOT_Implementation_Plan.html",
            convert_to_gdoc=GDRIVE_CONVERT_HTML_TO_GDOC,
        ))
    except Exception as exc:  # noqa: BLE001 — fail soft, never raise into the executor
        print(f"[google_drive] docs upload failed (non-fatal): {exc}")
    return urls


def upload_daily(date_str: str, paper_daily_path: str, summary_html_path: str | None = None) -> None:
    """Called after paper/lesson write, daily batch only. Fail soft."""
    if not ENABLE_GDRIVE_UPLOAD:
        return
    try:
        upload_or_replace(paper_daily_path, GDRIVE_FOLDER_DAILY_ID, f"paper_daily_{date_str}.json")
        if summary_html_path:
            upload_or_replace(summary_html_path, GDRIVE_FOLDER_DAILY_ID, f"summary_{date_str}.html")
    except Exception as exc:  # noqa: BLE001
        print(f"[google_drive] daily upload failed (non-fatal): {exc}")


def upload_lesson(date_str: str, lesson_text: str) -> None:
    if not ENABLE_GDRIVE_UPLOAD:
        return
    try:
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(lesson_text)
            tmp_path = f.name
        upload_or_replace(tmp_path, GDRIVE_FOLDER_LESSONS_ID, f"lesson_{date_str}.txt")
    except Exception as exc:  # noqa: BLE001
        print(f"[google_drive] lesson upload failed (non-fatal): {exc}")


if __name__ == "__main__":
    # S1 acceptance (Impl Plan §2B): manual docs push, prints two Drive URLs.
    req_html = Path(__file__).parent.parent.parent / "output" / "VIX_Trader_BOT_Requirements.html"
    plan_html = Path(__file__).parent.parent.parent / "output" / "VIX_Trader_BOT_Implementation_Plan.html"
    if not req_html.exists() or not plan_html.exists():
        print(f"HTML docs not found at {req_html} / {plan_html} — export them first, then rerun.")
    else:
        urls = upload_docs_bundle(str(req_html), str(plan_html))
        for u in urls:
            print(u)
