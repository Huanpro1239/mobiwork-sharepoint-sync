"""Publish one verified FIT month-to-date workbook to the approved SharePoint folder."""
from __future__ import annotations

import io
import os
import re
import tempfile
import time
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree

import requests

ORIGIN = "https://erp-nhan-hang.nguyensihuan711994.workers.dev"
ROOT = "/api/integrations/receiving"
HOST = "vikodacomvn.sharepoint.com"
SITE = "/sites/Planning"
DRIVE = "b!2K9oEC8dsUa6BquPvDyH35k3WNCRWItKlw8AJ2EKa5VahAhoPyTCQLc0Qsq_5UdO"
FOLDER = "Nhan hang"
MAX_BYTES = 25 * 1024 * 1024
ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
SHEETS = ["Nhận hàng", "Sản phẩm", "Lô hàng", "Khu vực kho", "Hóa đơn NCC"]


def validate_workbook(content: bytes) -> None:
    if not content or len(content) > MAX_BYTES:
        raise ValueError("Invalid FIT workbook size")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > 1000 or sum(i.file_size for i in entries) > 128 * 1024 * 1024:
            raise ValueError("FIT workbook package exceeds safety bounds")
        if archive.testzip() is not None:
            raise ValueError("Corrupt FIT workbook")
        root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        names = [e.attrib["name"] for e in root.findall("{*}sheets/{*}sheet")]
        if names != SHEETS:
            raise ValueError("FIT workbook does not contain the five full-detail sheets")


class FITExportClient:
    def __init__(self, client_id, client_secret, session=None, clock=time.monotonic, sleep=time.sleep):
        if not client_id or not client_secret:
            raise ValueError("FIT Access service credentials are not configured")
        self.session = session or requests.Session()
        self.headers = {"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": client_secret,
                        "User-Agent": "FITAutoReport/1.0"}
        self.clock, self.sleep = clock, sleep

    def request(self, method, path, key=None, stream=False):
        pattern = re.escape(ROOT) + (r"$" if method == "POST" else rf"/{ID}(?:/file)?$")
        if not re.fullmatch(pattern, path):
            raise ValueError("Unapproved FIT integration path")
        headers = dict(self.headers)
        if key:
            headers.update({"Idempotency-Key": key, "Content-Type": "application/json"})
        response = self.session.request(
            method, ORIGIN + path, headers=headers, data="{}" if method == "POST" else None,
            allow_redirects=False, timeout=(15, 90), stream=stream,
        )
        if response.status_code not in (200, 202, 409):
            response.close()
            raise RuntimeError(f"FIT integration returned HTTP {response.status_code}; SharePoint was not updated")
        return response

    def export(self, key, timeout=7200):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key):
            raise ValueError("Invalid idempotency key")
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            with self.request("POST", ROOT, key) as response:
                if response.status_code != 409:
                    job = response.json()
                    break
            self.sleep(30)
        else:
            raise TimeoutError("FIT stayed busy; SharePoint was not updated")
        job_id = job.get("id", "")
        if not re.fullmatch(ID, job_id):
            raise ValueError("Invalid FIT job ID")
        start, end = job.get("start", ""), job.get("end", "")
        if not re.fullmatch(r"20\d{2}-\d{2}-01", start) or not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", end) or start[:7] != end[:7] or date.fromisoformat(end) < date.fromisoformat(start):
            raise ValueError("Invalid FIT export period")
        while self.clock() < deadline:
            with self.request("GET", f"{ROOT}/{job_id}") as response:
                response.raise_for_status()
                status = response.json()
            if status.get("id") != job_id or status.get("start") != start or status.get("end") != end:
                raise ValueError("FIT changed the requested job or period")
            if status.get("phase") == "ready":
                return self.download(job_id), f"Nhan_hang_{start[:4]}_{start[5:7]}.xlsx"
            if status.get("phase") != "running":
                raise RuntimeError(f"FIT export {status.get('phase', 'unknown')}; confirm the ERP session/status on the website. SharePoint was not updated")
            self.sleep(15)
        raise TimeoutError("FIT export did not finish within the allowed time; SharePoint was not updated")

    def download(self, job_id):
        with self.request("GET", f"{ROOT}/{job_id}/file", stream=True) as response:
            response.raise_for_status()
            if "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" not in response.headers.get("Content-Type", ""):
                raise ValueError("FIT did not return an Excel workbook")
            content = bytearray()
            for chunk in response.iter_content(64 * 1024):
                content.extend(chunk)
                if len(content) > MAX_BYTES:
                    raise ValueError("FIT workbook is too large")
        validate_workbook(bytes(content))
        return bytes(content)


def publish(client, content, filename):
    if not re.fullmatch(r"Nhan_hang_20\d{2}_(0[1-9]|1[0-2])\.xlsx", filename):
        raise ValueError("Unapproved monthly workbook name")
    validate_workbook(content)
    folder = client.get_item_by_path(DRIVE, FOLDER)
    if not folder or "folder" not in folder:
        raise RuntimeError("The approved Planning/Nhan hang folder is unavailable")
    with tempfile.TemporaryDirectory(prefix="fit-receiving-") as directory:
        local = Path(directory) / filename
        local.write_bytes(content)
        result = client.upload_file(DRIVE, local, FOLDER)
        if not result.get("semantic_match"):
            raise RuntimeError("SharePoint workbook verification failed")
    return result


def main():
    from sharepoint_semantic import SemanticSharePointClient

    client = FITExportClient(os.environ.get("FIT_CF_ACCESS_CLIENT_ID"), os.environ.get("FIT_CF_ACCESS_CLIENT_SECRET"))
    key = "fit-" + os.environ["GITHUB_RUN_ID"] + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    content, filename = client.export(key)
    graph = SemanticSharePointClient(HOST, SITE, "Tài liệu")
    result = publish(graph, content, filename)
    print(f"Updated {FOLDER}/{filename}; SharePoint verification: {result['verification_mode']}")


if __name__ == "__main__":
    main()
