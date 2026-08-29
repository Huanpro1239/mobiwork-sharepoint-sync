from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from typing import Any

import pandas as pd

from monthly_master import master_filename
from mobiwork import ReportConfig


LOG = logging.getLogger("mobiwork_sync")


@dataclass
class SharePointMonthlyImageSource:
    """Expose SharePoint monthly report workbooks through fetch_report_range().

    Image sync needs the MobiWork HTTP session only for downloading image bytes. The
    image metadata itself is read from the monthly Excel master that the normal report
    sync has already written to SharePoint. This guarantees that report rows and image
    rows come from the same persisted source of truth.
    """

    mobiwork: Any
    sharepoint: Any
    drive_id: str
    source_files: list[str] = field(default_factory=list)

    @property
    def session(self) -> Any:
        return self.mobiwork.session

    @staticmethod
    def _month_starts(from_date: date, to_date: date) -> list[date]:
        if to_date < from_date:
            raise ValueError("to_date must be on or after from_date")

        months: list[date] = []
        current = from_date.replace(day=1)
        end = to_date.replace(day=1)
        while current <= end:
            months.append(current)
            if current.month == 12:
                current = date(current.year + 1, 1, 1)
            else:
                current = date(current.year, current.month + 1, 1)
        return months

    @staticmethod
    def _is_report_workbook(name: str, report_name: str) -> bool:
        lowered = name.casefold()
        return (
            lowered.endswith(".xlsx")
            and lowered.startswith(report_name.casefold())
            and not lowered.startswith("__sync_")
        )

    def _resolve_monthly_path(self, cfg: ReportConfig, month_start: date) -> str:
        remote_folder = f"{cfg.folder}/{month_start:%Y}/{month_start:%m}"
        canonical_name = master_filename(cfg.name, month_start)
        canonical_path = f"{remote_folder}/{canonical_name}"

        canonical = self.sharepoint.get_item_by_path(self.drive_id, canonical_path)
        if canonical and "folder" not in canonical:
            return canonical_path

        # Compatibility for months written before the single-month-master migration.
        # Prefer a full-month History workbook, then any report workbook in the folder.
        candidates: list[str] = []
        for item in self.sharepoint.list_folder_children(self.drive_id, remote_folder):
            if "folder" in item:
                continue
            name = str(item.get("name", "")).strip()
            if self._is_report_workbook(name, cfg.name):
                candidates.append(name)

        if not candidates:
            raise FileNotFoundError(
                f"No SharePoint workbook found for report={cfg.key} month={month_start:%Y-%m} "
                f"under {remote_folder}"
            )

        month_token = month_start.strftime("%Y-%m")
        history_prefix = f"{cfg.name}_History_{month_token}-01_to_".casefold()
        candidates.sort(
            key=lambda name: (
                name.casefold().startswith(history_prefix),
                name == canonical_name,
                name,
            ),
            reverse=True,
        )
        selected = candidates[0]
        selected_path = f"{remote_folder}/{selected}"
        LOG.warning(
            "Canonical monthly master is missing; using compatible SharePoint workbook: %s",
            selected_path,
        )
        return selected_path

    @staticmethod
    def _records_from_excel(content: bytes, remote_path: str) -> list[dict[str, Any]]:
        if not content:
            raise ValueError(f"SharePoint workbook is empty: {remote_path}")

        frame = pd.read_excel(BytesIO(content), sheet_name="Data", engine="openpyxl")
        # Convert pandas missing values to None so downstream field parsing is stable.
        frame = frame.astype(object).where(pd.notna(frame), None)
        return frame.to_dict(orient="records")

    def fetch_report_range(
        self,
        cfg: ReportConfig,
        from_date: date,
        to_date: date,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        self.source_files.clear()

        for month_start in self._month_starts(from_date, to_date):
            remote_path = self._resolve_monthly_path(cfg, month_start)
            LOG.info("Reading image metadata from SharePoint: %s", remote_path)
            content = self.sharepoint.download_file_bytes(self.drive_id, remote_path)
            if content is None:
                raise FileNotFoundError(
                    f"SharePoint workbook disappeared before download: {remote_path}"
                )
            month_records = self._records_from_excel(content, remote_path)
            records.extend(month_records)
            self.source_files.append(remote_path)
            LOG.info(
                "Loaded SharePoint image source rows: month=%s rows=%s file=%s",
                month_start.strftime("%Y-%m"),
                len(month_records),
                remote_path,
            )

        return records
