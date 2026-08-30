from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

import pandas as pd

from image_sync import ImageSyncConfig, _iter_urls, _parse_date, _remote_image_path
from kpi.kpi_rules import is_truthy


LOG = logging.getLogger("dms_ai_kpi")
_YEAR_RE = re.compile(r"^\d{4}$")
_MONTH_RE = re.compile(r"^(0[1-9]|1[0-2])$")
VN_TIMEZONE = "Asia/Ho_Chi_Minh"


@dataclass(frozen=True)
class KPIInputBundle:
    visits: pd.DataFrame
    orders: pd.DataFrame
    visit_sources: tuple[str, ...]
    order_sources: tuple[str, ...]
    warnings: tuple[str, ...]


def _month_start(now: datetime | pd.Timestamp) -> pd.Timestamp:
    stamp = pd.Timestamp(now)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert(VN_TIMEZONE).tz_localize(None)
    return stamp.replace(day=1).normalize()


def _visit_business_dates(visits: pd.DataFrame) -> pd.Series:
    source = visits["_sync_date"] if "_sync_date" in visits.columns else visits["ngay"]

    def parse(value: object):
        parsed = _parse_date(value)
        return pd.Timestamp(parsed) if parsed is not None else pd.NaT

    return source.map(parse)


class SharePointMonthlyKPISource:
    """Read all persisted monthly masters needed for exact new/old history."""

    def __init__(self, sharepoint, drive_id: str, reports: Iterable[Any]):
        self.sharepoint = sharepoint
        self.drive_id = drive_id
        self.reports = {report.key: report for report in reports}
        if "visit" not in self.reports or "order" not in self.reports:
            raise ValueError("KPI requires enabled visit and order reports")
        self._folder_cache: dict[str, list[dict[str, Any]]] = {}

    def _children(self, folder: str) -> list[dict[str, Any]]:
        if folder not in self._folder_cache:
            self._folder_cache[folder] = self.sharepoint.list_folder_children(
                self.drive_id, folder
            )
        return self._folder_cache[folder]

    @staticmethod
    def _canonical_name(report, year: int, month: int) -> str:
        return f"{report.name}_{year:04d}-{month:02d}.xlsx"

    def _discover_report_workbooks(
        self, report_key: str, through: pd.Timestamp
    ) -> list[str]:
        report = self.reports[report_key]
        result: list[str] = []
        for year_item in self._children(report.folder):
            year_name = str(year_item.get("name", "")).strip()
            if "folder" not in year_item or not _YEAR_RE.fullmatch(year_name):
                continue
            year = int(year_name)
            if year > through.year:
                continue
            year_folder = f"{report.folder}/{year_name}"
            for month_item in self._children(year_folder):
                month_name = str(month_item.get("name", "")).strip()
                if "folder" not in month_item or not _MONTH_RE.fullmatch(month_name):
                    continue
                month = int(month_name)
                if (year, month) > (through.year, through.month):
                    continue
                month_folder = f"{year_folder}/{month_name}"
                canonical = self._canonical_name(report, year, month)
                exact = next(
                    (
                        item
                        for item in self._children(month_folder)
                        if "folder" not in item
                        and str(item.get("name", "")).strip() == canonical
                    ),
                    None,
                )
                if exact:
                    result.append(f"{month_folder}/{canonical}")
                    continue
                candidates = sorted(
                    str(item.get("name", "")).strip()
                    for item in self._children(month_folder)
                    if "folder" not in item
                    and str(item.get("name", "")).casefold().startswith(
                        report.name.casefold()
                    )
                    and str(item.get("name", "")).casefold().endswith(".xlsx")
                    and not str(item.get("name", "")).startswith("__sync_")
                )
                if candidates:
                    selected = candidates[-1]
                    LOG.warning(
                        "Canonical monthly master missing; using %s/%s",
                        month_folder,
                        selected,
                    )
                    result.append(f"{month_folder}/{selected}")
        return sorted(result)

    def _read_excel(self, remote_path: str, sheet_name: str) -> pd.DataFrame:
        content = self.sharepoint.download_file_bytes(self.drive_id, remote_path)
        if content is None:
            raise FileNotFoundError(remote_path)
        return pd.read_excel(BytesIO(content), sheet_name=sheet_name, engine="openpyxl")

    def load(self, now: datetime | pd.Timestamp) -> KPIInputBundle:
        through = pd.Timestamp(now)
        visit_paths = self._discover_report_workbooks("visit", through)
        order_paths = self._discover_report_workbooks("order", through)
        if not visit_paths:
            raise FileNotFoundError(
                "Không tìm thấy monthly master báo cáo viếng thăm trên SharePoint"
            )

        visits = [self._read_excel(path, "Data") for path in visit_paths]
        orders: list[pd.DataFrame] = []
        promo_rows = 0
        for path in order_paths:
            frame = self._read_excel(path, "ChiTietSP")
            if "is_km" in frame.columns:
                promo = frame["is_km"].map(is_truthy)
                promo_rows += int(promo.sum())
                frame = frame.loc[~promo].copy()
            orders.append(frame)

        visit_frame = (
            pd.concat(visits, ignore_index=True, sort=False)
            if visits
            else pd.DataFrame()
        )
        order_frame = (
            pd.concat(orders, ignore_index=True, sort=False)
            if orders
            else pd.DataFrame()
        )
        defaults = {
            "ten_nhan_vien": "",
            "ma_kh": "",
            "ten_kh": "",
            "ngay": pd.NaT,
            "ghi_ton": False,
            "ghi_chu": "",
            "hinh_anh": "",
            "stt_hinh": "",
        }
        for column, default in defaults.items():
            if column not in visit_frame.columns:
                visit_frame[column] = default
        order_defaults = {
            "ma_kh": "",
            "ten_kh": "",
            "ngay_dat": pd.NaT,
            "ten_nguoi_dat": "",
            "ma_dvt": "",
            "so_luong": 0,
            "ma_phieu": "",
            "dien_giai": "",
        }
        for column, default in order_defaults.items():
            if column not in order_frame.columns:
                order_frame[column] = default

        warnings: list[str] = []
        if promo_rows:
            warnings.append(
                f"Đã loại {promo_rows:,} dòng sản phẩm khuyến mãi khỏi sản lượng KPI."
            )
        if not order_paths:
            warnings.append(
                "Chưa có monthly master đơn đặt hàng; điều kiện doanh số sẽ không đạt."
            )
        return KPIInputBundle(
            visits=visit_frame,
            orders=order_frame,
            visit_sources=tuple(visit_paths),
            order_sources=tuple(order_paths),
            warnings=tuple(warnings),
        )

    def recent_image_rows(
        self, visits: pd.DataFrame, now: datetime | pd.Timestamp
    ) -> list[dict[str, Any]]:
        current = _month_start(now)
        previous = current - pd.offsets.MonthBegin(1)
        next_month = current + pd.offsets.MonthBegin(1)
        dates = _visit_business_dates(visits)
        recent = visits.loc[(dates >= previous) & (dates < next_month)].copy()
        records: list[dict[str, Any]] = []
        for row in recent.to_dict(orient="records"):
            urls = list(_iter_urls(row.get("hinh_anh")))
            for index, url in enumerate(urls, start=1):
                item = dict(row)
                item["hinh_anh"] = url
                item["_image_index"] = index
                records.append(item)
        return records

    def resolve_image_path(
        self, row: dict[str, Any], cfg: ImageSyncConfig
    ) -> str:
        image_date = _parse_date(row.get("_sync_date")) or _parse_date(
            row.get(cfg.date_field)
        )
        if image_date is None:
            raise ValueError("Ảnh không có ngày hợp lệ")
        url = str(row.get(cfg.url_field, "")).strip()
        suffix = PurePosixPath(unquote(urlsplit(url).path)).suffix.casefold()
        if suffix == ".jpeg":
            suffix = ".jpg"
        if suffix not in {
            ".jpg",
            ".png",
            ".webp",
            ".gif",
            ".bmp",
            ".tif",
            ".tiff",
            ".heic",
            ".heif",
        }:
            suffix = ".jpg"
        folder, provisional = _remote_image_path(
            cfg,
            row,
            url,
            image_date,
            int(row.get("_image_index") or 1),
            suffix,
        )
        if self.sharepoint.get_item_by_path(self.drive_id, provisional):
            return provisional
        stem = PurePosixPath(provisional).stem
        matches = [
            str(item.get("name", "")).strip()
            for item in self._children(folder)
            if "folder" not in item
            and PurePosixPath(str(item.get("name", ""))).stem == stem
        ]
        if len(matches) == 1:
            return f"{folder}/{matches[0]}"
        if not matches:
            raise FileNotFoundError(f"Không tìm thấy ảnh đã sync: {provisional}")
        raise RuntimeError(f"Có nhiều file cùng identity ảnh: {folder}/{stem}")
