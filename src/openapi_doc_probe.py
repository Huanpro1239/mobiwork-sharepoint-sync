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
    except requests.RequestException:
        return ""
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


def described(node, trail="", out=None):
    """(path, description/enum/example) for every schema property named like a keyword."""
    out = [] if out is None else out
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{trail}.{key}"
            if any(k.casefold() == str(key).casefold() for k in KEYWORDS) and isinstance(value, dict):
                info = {k: value[k] for k in ("type", "description", "enum", "example", "items") if k in value}
                out.append(f"{here[-90:]} = {json.dumps(info, ensure_ascii=False)[:500]}")
            described(value, here, out)
    elif isinstance(node, list):
        for index, value in enumerate(node[:200]):
            described(value, f"{trail}[{index}]", out)
    return out


def spec_fields(text: str) -> None:
    try:
        spec = json.loads(text.lstrip('\ufeff'))
    except ValueError:
        return
    paths = spec.get("paths") or {}
    display = {p: m for p, m in paths.items() if "display" in p.casefold()}
    notice("OpenAPI paths", f"{len(paths)} paths; display: {list(display)[:6]}; "
           f"promotion: {[p for p in paths if 'promotion' in p.casefold()][:8]}")
    for path, methods in list(display.items())[:2]:
        notice(f"OpenAPI {path[-30:]}", json.dumps(methods, ensure_ascii=False))
    found = described(spec)
    notice("OpenAPI field docs", " ||| ".join(found[:12]) if found else "no keyword properties")
    for index in range(12, min(len(found), 36), 12):
        notice(f"OpenAPI field docs {index}", " ||| ".join(found[index:index + 12]))


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
    for keyword, width in (("soSuat", 600), ("gioiHan", 600)):
        parts = windows(joined, keyword, width=width, limit=1)
        notice(f"OpenAPI text {keyword}", " ||| ".join(parts) if parts else "not found")
    index = joined.find('"/OpenAPI/V1/DisplayData"')
    if index < 0:
        index = joined.find("/DisplayData")
    end = joined.find('"responses"', index)
    notice("OpenAPI DisplayData params", joined[index:end] if index >= 0 else "not found")
    index = joined.find('"/OpenAPI/V1/PromotionBonus"')
    end = joined.find('"responses"', index)
    notice("OpenAPI PromotionBonus params", joined[index:end] if index >= 0 else "not found")


if __name__ == "__main__":
    run()
