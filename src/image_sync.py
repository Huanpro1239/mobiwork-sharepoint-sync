from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

import requests

from mobiwork import MobiWorkClient, ReportConfig


LOG = logging.getLogger("mobiwork_sync")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_INVALID_SEGMENT_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ImageSyncConfig:
    enabled: bool = True
    source_report_key: str = "visit"
    root_folder: str = "Data anh"
    url_field: str = "hinh_anh"
    date_field: str = "ngay"
    employee_field: str = "ten_nhan_vien"
    customer_field: str = "ma_kh"
    sequence_field: str = "stt_hinh"
    require_ghi_ton: bool = False
    state_filename: str = "_state.json"
    request_timeout: int = 120
    max_download_retries: int = 3

    @classmethod
    def from_env(cls) -> "ImageSyncConfig":
        return cls(
            enabled=_env_bool("IMAGE_SYNC_ENABLED", True),
            source_report_key=(
                os.environ.get("IMAGE_SOURCE_REPORT", "visit").strip() or "visit"
            ),
            root_folder=(
                os.environ.get("IMAGE_ROOT_FOLDER", "Data anh").strip().strip("/")
                or "Data anh"
            ),
            url_field=os.environ.get("IMAGE_URL_FIELD", "hinh_anh").strip()
            or "hinh_anh",
            date_field=os.environ.get("IMAGE_DATE_FIELD", "ngay").strip() or "ngay",
            employee_field=(
                os.environ.get("IMAGE_EMPLOYEE_FIELD", "ten_nhan_vien").strip()
                or "ten_nhan_vien"
            ),
            customer_field=os.environ.get("IMAGE_CUSTOMER_FIELD", "ma_kh").strip()
            or "ma_kh",
            sequence_field=(
                os.environ.get("IMAGE_SEQUENCE_FIELD", "stt_hinh").strip()
                or "stt_hinh"
            ),
            require_ghi_ton=_env_bool("IMAGE_REQUIRE_GHI_TON", False),
            request_timeout=int(os.environ.get("IMAGE_REQUEST_TIMEOUT_SECONDS", "120")),
            max_download_retries=int(
                os.environ.get("IMAGE_MAX_DOWNLOAD_RETRIES", "3")
            ),
        )


def previous_month_start(today: date) -> date:
    first_this_month = today.replace(day=1)
    return (first_this_month - timedelta(days=1)).replace(day=1)


def retained_months(today: date) -> set[str]:
    return {
        today.strftime("%Y-%m"),
        previous_month_start(today).strftime("%Y-%m"),
    }


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value or "").strip()
    if not text:
        return None

    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y",
        "%d/%m/%Y %H:%M:%S",
        "%d-%m-%Y",
        "%d-%m-%Y %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _safe_segment(value: Any, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    text = _INVALID_SEGMENT_RE.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text or fallback)[:120]


def _iter_urls(value: Any) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, dict):
        for nested in value.values():
            yield from _iter_urls(nested)
        return
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _iter_urls(nested)
        return

    text = str(value).strip()
    if not text:
        return

    if text[:1] in {"[", "{"}:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None and parsed != value:
            yield from _iter_urls(parsed)
            return

    seen: set[str] = set()
    for match in _URL_RE.findall(text):
        url = match.rstrip(".,;)]}")
        if url not in seen:
            seen.add(url)
            yield url


def _looks_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "y",
        "x",
        "co",
        "có",
    }


def _content_type_and_extension(
    url: str,
    content: bytes,
    header_content_type: str | None,
) -> tuple[str, str]:
    content_type = (header_content_type or "").split(";", 1)[0].strip().lower()
    suffix = PurePosixPath(unquote(urlsplit(url).path)).suffix.lower()
    if suffix == ".jpeg":
        suffix = ".jpg"

    signatures = (
        (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
        (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
        (b"GIF87a", "image/gif", ".gif"),
        (b"GIF89a", "image/gif", ".gif"),
        (b"BM", "image/bmp", ".bmp"),
    )
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp", ".webp"
    for signature, detected_type, detected_ext in signatures:
        if content.startswith(signature):
            return detected_type, detected_ext

    if content_type.startswith("image/"):
        guessed = mimetypes.guess_extension(content_type) or ""
        if guessed == ".jpe":
            guessed = ".jpg"
        return content_type, guessed or (
            suffix if suffix in _IMAGE_EXTENSIONS else ".jpg"
        )

    if suffix in _IMAGE_EXTENSIONS:
        guessed_type = (
            mimetypes.guess_type(f"x{suffix}")[0] or "application/octet-stream"
        )
        return guessed_type, suffix

    return "application/octet-stream", ".jpg"


def _download_image(
    mobiwork: MobiWorkClient,
    url: str,
    timeout: int,
    max_retries: int,
) -> tuple[bytes, str, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Unsupported image URL: {url!r}")

    hostname = parsed.hostname.casefold()
    use_mobiwork_session = hostname == "mobiwork.vn" or hostname.endswith(
        ".mobiwork.vn"
    )
    session = mobiwork.session if use_mobiwork_session else requests.Session()

    try:
        for attempt in range(max_retries + 1):
            try:
                response = session.get(url, timeout=timeout)
                if (
                    response.status_code in {429, 500, 502, 503, 504}
                    and attempt < max_retries
                ):
                    delay = min(2.0 * (2**attempt), 30.0)
                    LOG.warning(
                        "Image download HTTP %s; retry %s/%s in %.1fs: %s",
                        response.status_code,
                        attempt + 1,
                        max_retries,
                        delay,
                        url,
                    )
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                content = response.content
                if not content:
                    raise ValueError(f"Downloaded image is empty: {url}")
                content_type, extension = _content_type_and_extension(
                    url,
                    content,
                    response.headers.get("Content-Type"),
                )
                return content, content_type, extension
            except (requests.Timeout, requests.ConnectionError):
                if attempt >= max_retries:
                    raise
                time.sleep(min(2.0 * (2**attempt), 30.0))
        raise RuntimeError("Unreachable image retry loop")
    finally:
        if not use_mobiwork_session:
            session.close()


def _state_path(cfg: ImageSyncConfig) -> str:
    return f"{cfg.root_folder}/{cfg.state_filename}"


def _resolve_start_date(today: date, state: dict[str, Any] | None) -> date:
    floor = previous_month_start(today)
    if not state:
        return floor

    last_date = _parse_date(state.get("last_successful_sync_date"))
    if not last_date:
        return floor

    # One-day overlap makes reruns resilient to late-arriving images.
    return max(floor, last_date - timedelta(days=1))


def _cleanup_old_months(
    sharepoint: Any,
    drive_id: str,
    cfg: ImageSyncConfig,
    today: date,
) -> list[str]:
    keep = retained_months(today)
    deleted: list[str] = []

    for item in sharepoint.list_folder_children(drive_id, cfg.root_folder):
        if "folder" not in item:
            continue
        name = str(item.get("name", "")).strip()
        if not _MONTH_RE.fullmatch(name) or name in keep:
            continue

        remote_path = f"{cfg.root_folder}/{name}"
        if sharepoint.delete_path(drive_id, remote_path):
            deleted.append(name)
            LOG.info("Removed expired image month folder: %s", remote_path)

    return sorted(deleted)


def _remote_image_path(
    cfg: ImageSyncConfig,
    record: dict[str, Any],
    image_url: str,
    image_date: date,
    image_index: int,
    extension: str,
) -> tuple[str, str]:
    employee = _safe_segment(
        record.get(cfg.employee_field),
        "Khong_ro_nhan_vien",
    )
    customer = _safe_segment(record.get(cfg.customer_field), "Khong_ma_KH")
    sequence = _safe_segment(record.get(cfg.sequence_field), str(image_index))
    digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:10]
    filename = f"{customer}_{image_date:%Y%m%d}_{sequence}_{digest}{extension}"
    remote_folder = (
        f"{cfg.root_folder}/{image_date:%Y-%m}/{employee}/{customer}"
    )
    return remote_folder, f"{remote_folder}/{filename}"


def run_image_sync(
    reports: list[ReportConfig],
    mobiwork: MobiWorkClient,
    sharepoint: Any | None,
    drive_id: str | None,
    dry_run: bool,
    today: date,
    cfg: ImageSyncConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or ImageSyncConfig.from_env()
    result: dict[str, Any] = {
        "enabled": cfg.enabled,
        "status": "disabled" if not cfg.enabled else "running",
        "root_folder": cfg.root_folder,
        "retained_months": sorted(retained_months(today)),
        "uploaded_count": 0,
        "skipped_existing_count": 0,
        "failed_count": 0,
        "candidate_count": 0,
        "records_scanned": 0,
        "deleted_month_folders": [],
    }
    if not cfg.enabled:
        return result

    source = next(
        (item for item in reports if item.key == cfg.source_report_key),
        None,
    )
    if source is None:
        result["status"] = "failed"
        result["error"] = (
            f"Image source report {cfg.source_report_key!r} is not enabled"
        )
        return result

    state: dict[str, Any] | None = None
    if not dry_run:
        if not sharepoint or not drive_id:
            result["status"] = "failed"
            result["error"] = "SharePoint client is unavailable for image sync"
            return result
        state = sharepoint.download_json(drive_id, _state_path(cfg))

    from_date = _resolve_start_date(today, state)
    result["from_date"] = from_date.isoformat()
    result["to_date"] = today.isoformat()

    records = mobiwork.fetch_report_range(source, from_date, today)
    result["records_scanned"] = len(records)

    planned: list[tuple[dict[str, Any], str, date, int]] = []
    for record in records:
        if cfg.require_ghi_ton and not _looks_true(record.get("ghi_ton")):
            continue

        record_date = _parse_date(record.get(cfg.date_field))
        if (
            record_date is None
            or record_date < previous_month_start(today)
            or record_date > today
        ):
            continue

        for image_index, image_url in enumerate(
            _iter_urls(record.get(cfg.url_field)),
            start=1,
        ):
            planned.append((record, image_url, record_date, image_index))

    result["candidate_count"] = len(planned)
    if dry_run:
        result["status"] = "dry_run"
        return result

    failures: list[dict[str, str]] = []

    for record, image_url, record_date, image_index in planned:
        try:
            url_suffix = PurePosixPath(
                unquote(urlsplit(image_url).path)
            ).suffix.lower()
            if url_suffix == ".jpeg":
                url_suffix = ".jpg"
            provisional_extension = (
                url_suffix if url_suffix in _IMAGE_EXTENSIONS else ".jpg"
            )
            remote_folder, provisional_path = _remote_image_path(
                cfg,
                record,
                image_url,
                record_date,
                image_index,
                provisional_extension,
            )

            existing = sharepoint.get_item_by_path(drive_id, provisional_path)
            if (
                existing
                and "folder" not in existing
                and int(existing.get("size") or 0) > 0
            ):
                result["skipped_existing_count"] += 1
                continue

            content, content_type, extension = _download_image(
                mobiwork,
                image_url,
                cfg.request_timeout,
                cfg.max_download_retries,
            )
            remote_folder, remote_path = _remote_image_path(
                cfg,
                record,
                image_url,
                record_date,
                image_index,
                extension,
            )

            if remote_path != provisional_path:
                existing = sharepoint.get_item_by_path(drive_id, remote_path)
                if (
                    existing
                    and "folder" not in existing
                    and int(existing.get("size") or 0) > 0
                ):
                    result["skipped_existing_count"] += 1
                    continue

            filename = remote_path.rsplit("/", 1)[-1]
            # SharePointClient._put_content already implements verified create/replace.
            sharepoint._put_content(
                drive_id,
                remote_folder,
                filename,
                content,
                content_type,
            )
            result["uploaded_count"] += 1
        except Exception as exc:
            result["failed_count"] += 1
            failures.append(
                {
                    "url": image_url[:500],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            LOG.exception("Unable to sync MobiWork image: %s", image_url)

    result["deleted_month_folders"] = _cleanup_old_months(
        sharepoint,
        drive_id,
        cfg,
        today,
    )

    sharepoint.upload_json(
        drive_id,
        _state_path(cfg),
        {
            "schema_version": 1,
            "last_successful_sync_date": today.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "root_folder": cfg.root_folder,
            "source_report": cfg.source_report_key,
            "retained_months": sorted(retained_months(today)),
            "failed_count": len(failures),
        },
    )

    if failures:
        result["status"] = "partial_failure"
        result["failures"] = failures[:50]
    else:
        result["status"] = "success"
    return result
