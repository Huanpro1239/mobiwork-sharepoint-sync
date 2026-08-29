from __future__ import annotations

from typing import Any

from sharepoint_semantic import SemanticSharePointClient


class ImageSharePointClient(SemanticSharePointClient):
    """SharePoint client with a public binary-upload contract for image sync.

    The base SharePoint implementation already has verified create/replace semantics.
    This adapter exposes that capability as a stable public method so image domain code
    never reaches into SharePoint private implementation details directly.
    """

    def upload_bytes(
        self,
        drive_id: str,
        remote_path: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        path = remote_path.strip("/")
        folder, separator, filename = path.rpartition("/")
        if not path or not filename or not separator:
            raise ValueError("remote_path must include a folder and file name")
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        if not content:
            raise ValueError("content must not be empty")
        if len(content) > 250 * 1024 * 1024:
            raise ValueError("binary upload exceeds the 250 MB simple-upload limit")
        media_type = content_type.strip() or "application/octet-stream"
        return self._put_content(
            drive_id,
            folder,
            filename,
            content,
            media_type,
        )
