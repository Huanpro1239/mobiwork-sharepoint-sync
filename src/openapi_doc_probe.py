"""Read-only probe of the public MobiWork OpenAPI documentation.

Prints, as GitHub notices, the documentation text around field names whose meaning
the calculator must not guess (display grading status, programme slots). Only public
API documentation is read; no credentials and no customer data are involved.
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urljoin

import requests

ROOTS = ["https://dms.mobiwork.vn/openapi/", "https://openapi.mobiwork.vn/"]
GUESSES = ["swagger.json", "openapi.json", "swagger.yaml", "openapi.yaml", "swagger-initializer.js",
           "v1/swagger.json", "docs.json", "api-docs"]
KEYWORDS = ["tt_cham_diem", "cham_diem", "DisplayData", "soSuat", "gioiHanCT", "time_soSuat", "soSuatCT"]


def notice(title: str, text: str) -> None:
    print(f"::notice title={title}::{' '.join(text.split())[:3800]}")


def fetch(session: requests.Session, url: str) -> str:
    try:
        response = session.get(url, timeout=30)
    except requests.RequestException as exc:
        notice("OpenAPI doc fetch", f"{url} -> {type(exc).__name__}")
        return ""
    kind = response.headers.get("content-type", "")
    notice("OpenAPI doc fetch", f"{url} -> {response.status_code} {kind[:40]} {len(response.text)} chars")
    return response.text if response.ok else ""


def linked_urls(base: str, text: str) -> list[str]:
    found = re.findall(r"""(?:src|href|url)\s*[:=]\s*["']([^"']+\.(?:json|ya?ml|js))["']""", text)
    found += re.findall(r"""["']([^"'\s]+(?:swagger|openapi|api-docs)[^"'\s]*\.(?:json|ya?ml))["']""", text)
    return [urljoin(base, item) for item in dict.fromkeys(found)]


def windows(text: str, keyword: str, width: int = 450, limit: int = 4) -> list[str]:
    out = []
    for match in re.finditer(re.escape(keyword), text):
        start = max(0, match.start() - width // 3)
        out.append(text[start:match.end() + width])
        if len(out) >= limit:
            break
    return out


def spec_fields(text: str) -> None:
    try:
        spec = json.loads(text)
    except ValueError:
        return
    paths = spec.get("paths") or {}
    for path, methods in paths.items():
        if "Display" in path or "PromotionBonus" in path:
            notice(f"OpenAPI path {path[:40]}", json.dumps(methods, ensure_ascii=False)[:3800])


def run() -> None:
    session = requests.Session()
    session.headers["User-Agent"] = "mobiwork-sharepoint-sync doc probe"
    seen: set[str] = set()
    queue = [url for root in ROOTS for url in [root] + [urljoin(root, g) for g in GUESSES]]
    corpus = []
    while queue and len(seen) < int(os.environ.get("DOC_PROBE_LIMIT", "30")):
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        text = fetch(session, url)
        if not text:
            continue
        corpus.append(text)
        spec_fields(text)
        queue += [u for u in linked_urls(url, text) if u not in seen and "mobiwork" in u]
    joined = "\n".join(corpus)
    for keyword in KEYWORDS:
        parts = windows(joined, keyword)
        notice(f"OpenAPI doc {keyword}", " ||| ".join(parts) if parts else "not found")


if __name__ == "__main__":
    run()
