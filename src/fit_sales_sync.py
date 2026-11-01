"""Publish FIT "ĐH Bán trong kỳ NPP" (Mức lấy Đơn hàng + Hóa Đơn) to SharePoint.

Worker erp-nhan-hang keeps the FIT session (keep-alive, CAPTCHA via Telegram /dangnhap) and
exports the native FIT workbook through a read-only route that only this service token may
call. Triggered by the Telegram command /banhang (Worker sends repository_dispatch
``fit_ban_hang``), by hand, or on schedule. Both files are written only after both exports
succeed; Telegram receives the files and a summary (send only, never getUpdates: the Worker
is the bot's only reader).

    python src/fit_sales_sync.py                         # đầu tháng -> hôm nay
    python src/fit_sales_sync.py --ky thangtruoc         # | 10/2026 | "01/10/2026 15/10/2026"
    python src/fit_sales_sync.py --start 2026-10-01 --end 2026-10-31
    python src/fit_sales_sync.py --dry-run               # không ghi SharePoint
"""
from __future__ import annotations

import argparse
import calendar
import io
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ORIGIN = "https://erp-nhan-hang.nguyensihuan711994.workers.dev"
ROUTE = "/api/integrations/ban-hang-npp/file"
HOST = "vikodacomvn.sharepoint.com"
SITE = "/sites/Planning"
DRIVE = "b!2K9oEC8dsUa6BquPvDyH35k3WNCRWItKlw8AJ2EKa5VahAhoPyTCQLc0Qsq_5UdO"
DEFAULT_FOLDER = "DH ban trong ky NPP"
MAX_BYTES = 45 * 1024 * 1024
VN = timezone(timedelta(hours=7))
XLSM_TYPE = "application/vnd.ms-excel.sheet.macroEnabled.12"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class SalesSyncError(RuntimeError):
    """Message safe to show in Telegram/logs (no secrets, no business data)."""


@dataclass(frozen=True)
class Report:
    muc: str        # Worker query value
    muc_lay: str    # "Mức lấy" label on FIT
    base: str       # SharePoint file name without extension (user's naming)
    title: str


REPORTS = (
    Report("don_hang", "Đơn hàng", "BCDonHangBanTrongKyNPP", "ĐH bán trong kỳ NPP – theo Đơn hàng"),
    Report("hoa_don", "Hóa Đơn", "Lay So Hoa don", "ĐH bán trong kỳ NPP – theo Hóa đơn"),
)


# ---- kỳ báo cáo ---------------------------------------------------------------------
def today_vn() -> date:
    return datetime.now(VN).date()


def _day(text: str) -> date:
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise SalesSyncError(f"Ngày không hợp lệ: {text!r}. Dùng dd/mm/yyyy.")


def _month(year: int, month: int, today: date) -> tuple[date, date]:
    if not 1 <= month <= 12:
        raise SalesSyncError("Tháng phải từ 1 đến 12.")
    first = date(year, month, 1)
    if first > today:
        raise SalesSyncError("Tháng này chưa bắt đầu.")
    return first, min(date(year, month, calendar.monthrange(year, month)[1]), today)


def parse_period(text: str, today: date | None = None) -> tuple[date, date]:
    """Same grammar as the Worker's Telegram command /banhang."""
    today = today or today_vn()
    raw = " ".join((text or "").split())
    low = raw.lower()
    if not raw:
        return today.replace(day=1), today
    if low in ("thangtruoc", "tháng trước", "thang truoc", "tt"):
        prev = today.replace(day=1) - timedelta(days=1)
        return _month(prev.year, prev.month, today)
    if m := re.fullmatch(r"(\d{1,2})[/-](\d{4})", raw):
        return _month(int(m[2]), int(m[1]), today)
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})", raw):
        return _month(int(m[1]), int(m[2]), today)
    parts = re.sub(r"\s*(?:->|→|–|—)\s*|\s+(?:-|den|đến)\s+", " ", raw).split()
    if len(parts) not in (1, 2):
        raise SalesSyncError("Kỳ không hợp lệ. Ví dụ: 10/2026 | thangtruoc | 01/10/2026 15/10/2026")
    start, end = _day(parts[0]), _day(parts[-1])
    if start > end:
        raise SalesSyncError("Từ ngày phải trước hoặc bằng Đến ngày.")
    if end > today:
        raise SalesSyncError("Đến ngày không được sau hôm nay.")
    if (end - start).days > 366:
        raise SalesSyncError("Kỳ báo cáo tối đa 1 năm.")
    return start, end


def fmt_period(start: date, end: date) -> str:
    return f"{start:%d/%m/%Y} – {end:%d/%m/%Y}"


# ---- Excel -----------------------------------------------------------------------------
def workbook_extension(content: bytes) -> str:
    """'.xlsm' for FIT's macro-enabled export, '.xlsx' otherwise; reject non-workbooks."""
    if not content or len(content) > MAX_BYTES:
        raise SalesSyncError("FIT trả file rỗng hoặc quá lớn.")
    if content[:8] == bytes.fromhex("D0CF11E0A1B11AE1"):
        return ".xls"
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            if len(names) > 2000 or not {"[Content_Types].xml", "xl/workbook.xml"} <= names:
                raise SalesSyncError("FIT trả nội dung không phải Excel.")
            types = archive.read("[Content_Types].xml")
    except zipfile.BadZipFile:
        raise SalesSyncError("FIT trả nội dung không phải Excel (có thể là trang lỗi).") from None
    return ".xlsm" if b"macroEnabled" in types else ".xlsx"


# ---- Worker ------------------------------------------------------------------------------
class SalesExportClient:
    def __init__(self, client_id, client_secret, session=None, sleep=time.sleep):
        if not client_id or not client_secret:
            raise SalesSyncError("Thiếu secret FIT_CF_ACCESS_CLIENT_ID / FIT_CF_ACCESS_CLIENT_SECRET.")
        self.session = session or requests.Session()
        self.headers = {"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": client_secret,
                        "User-Agent": "FITAutoReport/1.0 (ban-hang-npp)"}
        self.sleep = sleep

    def fetch(self, report: Report, start: date, end: date, attempts: int = 12) -> bytes:
        params = {"start": start.isoformat(), "end": end.isoformat(), "muc": report.muc}
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.get(ORIGIN + ROUTE, params=params, headers=self.headers,
                                            allow_redirects=False, timeout=(15, 150))
            except requests.RequestException as exc:
                if attempt == attempts:
                    raise SalesSyncError(f"Không kết nối được Worker ({type(exc).__name__}).") from None
                self.sleep(20)
                continue
            if response.status_code == 200:
                content = response.content
                workbook_extension(content)
                print(f"[Worker] {report.muc}: {len(content):,} byte")
                return content
            info = _json(response)
            if response.status_code == 409 or info.get("code") == "ERP_BUSY":
                if attempt < attempts:
                    print(f"[Worker] FIT đang bận ({attempt}/{attempts}), chờ 30s")
                    self.sleep(30)
                    continue
                raise SalesSyncError("FIT đang bận xuất dữ liệu khác quá lâu. Thử lại sau.")
            if response.status_code == 401 or info.get("code") == "ERP_LOGIN_REQUIRED":
                raise SalesSyncError("Phiên FIT đã hết hạn. Gửi /dangnhap cho bot, trả lời mã rồi gửi lại /banhang.")
            if response.status_code in (301, 302, 303, 307, 308, 403) and not info:
                raise SalesSyncError("Cloudflare Access từ chối: kiểm tra service token và Worker đã deploy bản có /banhang.")
            if response.status_code in (502, 503, 504) and attempt < attempts:
                self.sleep(20)
                continue
            detail = f" – {info['error']}" if info.get("error") else ""
            raise SalesSyncError(f"{report.title}: Worker trả HTTP {response.status_code}{detail}")
        raise SalesSyncError("Không lấy được báo cáo từ Worker.")


def _json(response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ---- Telegram (send only) --------------------------------------------------------------
class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, session=None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat_id = chat_id
        self.session = session or requests.Session()

    def _post(self, method, data, files=None):
        try:
            response = self.session.post(self.base + method, data=data, files=files, timeout=120)
            ok = response.json().get("ok")
        except (requests.RequestException, ValueError):
            ok = False
        if not ok:
            print(f"[Telegram] {method} không thành công")  # never print the URL (contains token)

    def message(self, text: str):
        self._post("sendMessage", {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": "true"})

    def document(self, name: str, content: bytes, caption: str):
        self._post("sendDocument", {"chat_id": self.chat_id, "caption": caption[:1000]},
                   {"document": (name, content, "application/octet-stream")})


def notifier_from_env():
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and re.fullmatch(r"-?\d{3,20}", chat):
        return TelegramNotifier(token, chat)
    return None


# ---- SharePoint ------------------------------------------------------------------------
def publish(graph, folder: str, name: str, content: bytes) -> dict:
    if not re.fullmatch(r"(BCDonHangBanTrongKyNPP|Lay So Hoa don)\.xls[xm]?", name):
        raise ValueError("Unapproved sales workbook name")
    content_type = XLSM_TYPE if name.endswith(".xlsm") else XLSX_TYPE
    return graph._put_content(DRIVE, folder, name, content, content_type)


def run(start: date, end: date, *, client, graph_factory, notifier=None, folder=DEFAULT_FOLDER,
        dry_run=False) -> int:
    def say(text):
        print("[Bot] " + text.splitlines()[0])
        if notifier:
            notifier.message(text)

    try:
        files = []
        for report in REPORTS:  # fetch both before touching SharePoint
            content = client.fetch(report, start, end)
            files.append((report, report.base + workbook_extension(content), content))
        lines = []
        if dry_run:
            for _, name, content in files:
                Path(name).write_bytes(content)
                lines.append(f"{name} (dry-run)")
        else:
            graph = graph_factory()
            for _, name, content in files:
                result = publish(graph, folder, name, content)
                print(f"[SharePoint] {folder}/{name}: {result.get('verification_mode', 'uploaded')}")
                lines.append(f"{name}: {result.get('webUrl', '')}")
        if notifier:
            for report, name, content in files:
                notifier.document(name, content, f"{report.title} | {fmt_period(start, end)}")
        say(f"✅ ĐH bán trong kỳ NPP {fmt_period(start, end)} đã lên SharePoint\nThư mục: {folder}\n" + "\n".join(lines))
        return 0
    except SalesSyncError as exc:
        say(f"❌ ĐH bán trong kỳ NPP {fmt_period(start, end)}: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - report type only
        print(f"[Bot] {type(exc).__name__}: {str(exc)[:300]}")
        say(f"❌ ĐH bán trong kỳ NPP {fmt_period(start, end)}: lỗi {type(exc).__name__}, xem log GitHub Actions.")
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="FIT ĐH Bán trong kỳ NPP -> SharePoint")
    parser.add_argument("--ky", default="")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    notifier = notifier_from_env()
    try:
        if args.start:
            start, end = parse_period(f"{args.start} {args.end or args.start}")
        else:
            start, end = parse_period(args.ky)
        client = SalesExportClient(os.environ.get("FIT_CF_ACCESS_CLIENT_ID"),
                                   os.environ.get("FIT_CF_ACCESS_CLIENT_SECRET"))
    except SalesSyncError as exc:
        print(f"[Lỗi] {exc}", file=sys.stderr)
        if notifier:
            notifier.message(f"❌ /banhang: {exc}")
        return 2

    def graph_factory():
        from sharepoint_semantic import SemanticSharePointClient

        return SemanticSharePointClient(HOST, SITE, "Tài liệu")

    folder = (os.environ.get("FIT_SALES_SP_FOLDER") or DEFAULT_FOLDER).strip("/")
    return run(start, end, client=client, graph_factory=graph_factory, notifier=notifier,
               folder=folder, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
