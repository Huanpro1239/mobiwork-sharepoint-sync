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
from typing import Any, Iterable, Protocol
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import requests

from mobiwork import ReportConfig

LOG = logging.getLogger("mobiwork_sync")
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
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


class ImageMetadataSource(Protocol):
    @property
    def session(self) -> requests.Session: ...

    def fetch_report_range(
        self,
        cfg: ReportConfig,
        from_date: date,
        to_date: date,
    ) -> list[dict[str, Any]]: ...


class ImageStorage(Protocol):
    def download_json(self, drive_id: str, remote_path: str) -> dict[str, Any] | None: ...
    def list_folder_children(self, drive_id: str, remote_folder: str) -> list[dict[str, Any]]: ...
    def delete_path(self, drive_id: str, remote_path: str) -> bool: ...
    def get_item_by_path(self, drive_id: str, remote_path: str) -> dict[str, Any] | None: ...
    def upload_bytes(
        self,
        drive_id: str,
        remote_path: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]: ...
    def upload_json(
        self,
        drive_id: str,
        remote_path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _parse_hosts(value: str) -> tuple[str, ...]:
    hosts = {
        item.strip().casefold().lstrip(".")
        for item in value.split(",")
        if item.strip()
    }
    return tuple(sorted(hosts))


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
    request_timeout: int = 30
    max_download_retries: int = 2
    max_image_bytes: int = 20 * 1024 * 1024
    allowed_hosts: tuple[str, ...] = ("dmsimages.mobiwork.vn", "mobiwork.vn")
    force_from_date: date | None = None

    def __post_init__(self) -> None:
        if not self.root_folder.strip("/"):
            raise ValueError("IMAGE_ROOT_FOLDER must not be empty")
        if self.request_timeout < 1:
            raise ValueError("IMAGE_REQUEST_TIMEOUT_SECONDS must be >= 1")
        if not 0 <= self.max_download_retries <= 10:
            raise ValueError("IMAGE_MAX_DOWNLOAD_RETRIES must be between 0 and 10")
        if not 1 <= self.max_image_bytes <= 100 * 1024 * 1024:
            raise ValueError("IMAGE_MAX_BYTES must be between 1 byte and 100 MB")
        if not self.allowed_hosts:
            raise ValueError("IMAGE_ALLOWED_HOSTS must contain at least one host")

    @classmethod
    def from_env(cls) -> "ImageSyncConfig":
        force_text = os.environ.get("IMAGE_FORCE_FROM_DATE", "").strip()
        force_from_date = _parse_date(force_text) if force_text else None
        if force_text and force_from_date is None:
            raise ValueError("IMAGE_FORCE_FROM_DATE must be a valid date")

        root_folder = (
            os.environ.get("IMAGE_ROOT_FOLDER", "Data anh")
            .strip()
            .strip("/")
            or "Data anh"
        )
        url_field = os.environ.get("IMAGE_URL_FIELD", "hinh_anh").strip() or "hinh_anh"
        date_field = os.environ.get("IMAGE_DATE_FIELD", "ngay").strip() or "ngay"
        employee_field = (
            os.environ.get("IMAGE_EMPLOYEE_FIELD", "ten_nhan_vien")
            .strip()
            or "ten_nhan_vien"
        )
        customer_field = os.environ.get("IMAGE_CUSTOMER_FIELD", "ma_kh").strip() or "ma_kh"
        sequence_field = os.environ.get("IMAGE_SEQUENCE_FIELD", "stt_hinh").strip() or "stt_hinh"
        max_image_bytes = int(os.environ.get("IMAGE_MAX_BYTES", str(20 * 1024 * 1024)))
        allowed_hosts = _parse_hosts(
            os.environ.get(
                "IMAGE_ALLOWED_HOSTS",
                "dmsimages.mobiwork.vn,mobiwork.vn",
            )
        )

        return cls(
            enabled=_env_bool("IMAGE_SYNC_ENABLED", True),
            source_report_key=os.environ.get("IMAGE_SOURCE_REPORT", "visit").strip() or "visit",
            root_folder=root_folder,
            url_field=url_field,
            date_field=date_field,
            employee_field=employee_field,
            customer_field=customer_field,
            sequence_field=sequence_field,
            require_ghi_ton=_env_bool("IMAGE_REQUIRE_GHI_TON", False),
            request_timeout=int(os.environ.get("IMAGE_REQUEST_TIMEOUT_SECONDS", "30")),
            max_download_retries=int(os.environ.get("IMAGE_MAX_DOWNLOAD_RETRIES", "2")),
            max_image_bytes=max_image_bytes,
            allowed_hosts=allowed_hosts,
            force_from_date=force_from_date,
        )


@dataclass(frozen=True)
class ImageCandidate:
    record: dict[str, Any]
    url: str
    image_date: date
    image_index: int


def previous_month_start(today: date) -> date:
    first_this_month = today.replace(day=1)
    return (first_this_month - timedelta(days=1)).replace(day=1)


def retained_months(today: date) -> set[str]:
    return {today.strftime("%Y-%m"), previous_month_start(today).strftime("%Y-%m")}


_ISO_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _local_date(value: datetime) -> date:
    if value.tzinfo is not None:
        return value.astimezone(VN_TZ).date()
    return value.date()


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return _local_date(value)
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    match = _ISO_DATE_PREFIX_RE.match(text)
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            pass
    normalized = text.replace("Z", "+00:00")
    try:
        return _local_date(datetime.fromisoformat(normalized))
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
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "x", "co", "có"}


def _host_allowed(hostname: str, allowed_hosts: tuple[str, ...]) -> bool:
    host = hostname.strip().casefold().rstrip(".")
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts)


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
        return content_type, guessed or (suffix if suffix in _IMAGE_EXTENSIONS else ".jpg")

    # An explicit non-image media type is stronger evidence than the URL suffix.
    # This prevents HTML login/error pages served from a *.jpg URL from being
    # persisted as fake images. Generic/missing types may still fall back to suffix.
    if content_type and content_type not in {
        "application/octet-stream",
        "binary/octet-stream",
    }:
        return "application/octet-stream", ".jpg"
    if suffix in _IMAGE_EXTENSIONS:
        guessed_type = (
            mimetypes.guess_type(f"x{suffix}")[0] or "application/octet-stream"
        )
        return guessed_type, suffix
    return "application/octet-stream", ".jpg"


def _download_image(
    source: ImageMetadataSource,
    url: str,
    cfg: ImageSyncConfig,
) -> tuple[bytes, str, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Unsupported image URL: {url!r}")
    if not _host_allowed(parsed.hostname, cfg.allowed_hosts):
        raise ValueError(f"Image host is not allow-listed: {parsed.hostname}")

    hostname = parsed.hostname.casefold()
    use_source_session = hostname == "mobiwork.vn" or hostname.endswith(".mobiwork.vn")
    session = source.session if use_source_session else requests.Session()
    try:
        for attempt in range(cfg.max_download_retries + 1):
            response: requests.Response | None = None
            try:
                response = session.get(
                    url,
                    timeout=cfg.request_timeout,
                    stream=True,
                    allow_redirects=True,
                )
                if (
                    response.status_code in {429, 500, 502, 503, 504}
                    and attempt < cfg.max_download_retries
                ):
                    delay = min(2.0 * (2**attempt), 30.0)
                    LOG.warning(
                        "Image download HTTP %s; retry %s/%s in %.1fs: %s",
                        response.status_code,
                        attempt + 1,
                        cfg.max_download_retries,
                        delay,
                        url,
                    )
                    time.sleep(delay)
                    continue
                response.raise_for_status()

                final_url = str(response.url or url)
                final_host = urlsplit(final_url).hostname
                if not final_host or not _host_allowed(final_host, cfg.allowed_hosts):
                    raise ValueError(
                        "Image redirect target is not allow-listed: "
                        + (final_host or "<missing>")
                    )

                declared_length = response.headers.get("Content-Length")
                if (
                    declared_length
                    and declared_length.isdigit()
                    and int(declared_length) > cfg.max_image_bytes
                ):
                    raise ValueError(
                        "Image exceeds configured size limit: "
                        + str(declared_length)
                        + " > "
                        + str(cfg.max_image_bytes)
                        + " bytes"
                    )

                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > cfg.max_image_bytes:
                        raise ValueError(
                            "Image exceeds configured size limit while streaming: "
                            + str(total)
                            + " > "
                            + str(cfg.max_image_bytes)
                            + " bytes"
                        )
                    chunks.append(chunk)
                content = b"".join(chunks)
                if not content:
                    raise ValueError(f"Downloaded image is empty: {url}")

                content_type, extension = _content_type_and_extension(
                    final_url,
                    content,
                    response.headers.get("Content-Type"),
                )
                if not content_type.startswith("image/"):
                    raise ValueError(
                        "Downloaded payload is not a recognized image: content_type="
                        + str(content_type)
                    )
                return content, content_type, extension
            except (requests.Timeout, requests.ConnectionError):
                if attempt >= cfg.max_download_retries:
                    raise
                time.sleep(min(2.0 * (2**attempt), 30.0))
            finally:
                if response is not None:
                    response.close()
        raise RuntimeError("Unreachable image retry loop")
    finally:
        if not use_source_session:
            session.close()


def _state_path(cfg: ImageSyncConfig) -> str:
    return f"{cfg.root_folder}/{cfg.state_filename}"


def _resolve_start_date(
    today: date,
    state: dict[str, Any] | None,
    force_from_date: date | None = None,
) -> date:
    floor = previous_month_start(today)
    if force_from_date is not None:
        if force_from_date > today:
            raise ValueError("IMAGE_FORCE_FROM_DATE cannot be after today")
        return max(floor, force_from_date)
    if not state:
        return floor
    cursor = state.get("last_completed_sync_date") or state.get("last_successful_sync_date")
    last_date = _parse_date(cursor)
    retry_from_date = _parse_date(state.get("retry_from_date"))
    if not last_date:
        return max(floor, retry_from_date) if retry_from_date else floor

    normal_start = max(floor, last_date - timedelta(days=1))
    if retry_from_date is None:
        return normal_start
    return max(floor, min(normal_start, retry_from_date))


def _cleanup_old_months(
    storage: ImageStorage,
    drive_id: str,
    cfg: ImageSyncConfig,
    today: date,
) -> list[str]:
    keep = retained_months(today)
    deleted: list[str] = []
    for item in storage.list_folder_children(drive_id, cfg.root_folder):
        if "folder" not in item:
            continue
        name = str(item.get("name", "")).strip()
        if not _MONTH_RE.fullmatch(name) or name in keep:
            continue
        remote_path = f"{cfg.root_folder}/{name}"
        if storage.delete_path(drive_id, remote_path):
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
    employee = _safe_segment(record.get(cfg.employee_field), "Khong_ro_nhan_vien")
    customer = _safe_segment(record.get(cfg.customer_field), "Khong_ma_KH")
    sequence = _safe_segment(record.get(cfg.sequence_field), str(image_index))
    digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:10]
    filename = f"{customer}_{image_date:%Y%m%d}_{sequence}_{digest}{extension}"
    remote_folder = f"{cfg.root_folder}/{image_date:%Y-%m}/{employee}/{customer}"
    return remote_folder, f"{remote_folder}/{filename}"


def _plan_candidates(
    records: list[dict[str, Any]],
    cfg: ImageSyncConfig,
    from_date: date,
    today: date,
) -> list[ImageCandidate]:
    planned: list[ImageCandidate] = []
    retention_floor = previous_month_start(today)
    for record in records:
        if cfg.require_ghi_ton and not _looks_true(record.get("ghi_ton")):
            continue
        record_date = (
            _parse_date(record.get("_sync_date"))
            or _parse_date(record.get(cfg.date_field))
        )
        if (
            record_date is None
            or record_date < from_date
            or record_date < retention_floor
            or record_date > today
        ):
            continue
        for image_index, image_url in enumerate(_iter_urls(record.get(cfg.url_field)), start=1):
            planned.append(ImageCandidate(record, image_url, record_date, image_index))
    return planned


def _existing_nonempty_file(storage: ImageStorage, drive_id: str, remote_path: str) -> bool:
    existing = storage.get_item_by_path(drive_id, remote_path)
    return bool(existing and "folder" not in existing and int(existing.get("size") or 0) > 0)


def run_image_sync(
    reports: list[ReportConfig],
    source: ImageMetadataSource,
    storage: ImageStorage | None,
    drive_id: str | None,
    dry_run: bool,
    today: date,
    cfg: ImageSyncConfig | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
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
        "downloaded_bytes": 0,
        "deleted_month_folders": [],
    }
    if not cfg.enabled:
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        return result

    report = next((item for item in reports if item.key == cfg.source_report_key), None)
    if report is None:
        result["status"] = "failed"
        result["error"] = f"Image source report {cfg.source_report_key!r} is not enabled"
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        return result

    state: dict[str, Any] | None = None
    if storage and drive_id:
        state = storage.download_json(drive_id, _state_path(cfg))
    elif not dry_run:
        result["status"] = "failed"
        result["error"] = "SharePoint storage is unavailable for image sync"
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        return result

    from_date = _resolve_start_date(today, state, cfg.force_from_date)
    result["from_date"] = from_date.isoformat()
    result["to_date"] = today.isoformat()
    result["forced_from_date"] = cfg.force_from_date.isoformat() if cfg.force_from_date else None

    records = source.fetch_report_range(report, from_date, today)
    result["records_scanned"] = len(records)
    planned = _plan_candidates(records, cfg, from_date, today)
    result["candidate_count"] = len(planned)
    if dry_run:
        result["status"] = "dry_run"
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        return result

    if storage is None or drive_id is None:
        raise RuntimeError("SharePoint storage unexpectedly unavailable")

    failures: list[dict[str, str]] = []
    for candidate in planned:
        try:
            url_suffix = PurePosixPath(unquote(urlsplit(candidate.url).path)).suffix.lower()
            if url_suffix == ".jpeg":
                url_suffix = ".jpg"
            provisional_extension = url_suffix if url_suffix in _IMAGE_EXTENSIONS else ".jpg"
            _, provisional_path = _remote_image_path(
                cfg,
                candidate.record,
                candidate.url,
                candidate.image_date,
                candidate.image_index,
                provisional_extension,
            )
            if _existing_nonempty_file(storage, drive_id, provisional_path):
                result["skipped_existing_count"] += 1
                continue

            content, content_type, extension = _download_image(source, candidate.url, cfg)
            _, remote_path = _remote_image_path(
                cfg,
                candidate.record,
                candidate.url,
                candidate.image_date,
                candidate.image_index,
                extension,
            )
            if (
                remote_path != provisional_path
                and _existing_nonempty_file(storage, drive_id, remote_path)
            ):
                result["skipped_existing_count"] += 1
                continue

            storage.upload_bytes(drive_id, remote_path, content, content_type)
            result["uploaded_count"] += 1
            result["downloaded_bytes"] += len(content)
        except Exception as exc:
            result["failed_count"] += 1
            failures.append(
                {
                    "url": candidate.url[:500],
                    "date": candidate.image_date.isoformat(),
                    "employee": str(candidate.record.get(cfg.employee_field) or "")[:160],
                    "customer": str(candidate.record.get(cfg.customer_field) or "")[:160],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            LOG.exception("Unable to sync MobiWork image: %s", candidate.url)

    result["deleted_month_folders"] = _cleanup_old_months(storage, drive_id, cfg, today)

    previous_successful = state.get("last_successful_sync_date") if state else None
    run_status = "partial_failure" if failures else "success"
    retry_from_date = min((item["date"] for item in failures), default=None)
    result["retry_from_date"] = retry_from_date
    storage.upload_json(
        drive_id,
        _state_path(cfg),
        {
            "schema_version": 3,
            "last_completed_sync_date": today.isoformat(),
            "last_successful_sync_date": today.isoformat() if not failures else previous_successful,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "root_folder": cfg.root_folder,
            "source_report": cfg.source_report_key,
            "source_mode": "sharepoint_monthly_master",
            "retained_months": sorted(retained_months(today)),
            "last_run_status": run_status,
            "failed_count": len(failures),
            "retry_from_date": retry_from_date,
        },
    )

    result["status"] = run_status
    if failures:
        result["failures"] = failures[:50]
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result