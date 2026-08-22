from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


DEFAULT_SOURCE_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1kd2FnZ3zQ0BXQuvoccIwbQub8O-zqAw2nT1vs4WPQwo/"
    "export?format=csv&gid=1772227210"
)
DEFAULT_MODEL = "openai/gpt-4o-mini"
MODELS_ENDPOINT = "https://models.github.ai/inference/chat/completions"
REQUIRED_SOURCE_COLUMNS = {
    "ma_nv",
    "ten_nhan_vien",
    "ngay",
    "ma_kh",
    "ten_kh",
    "loai_kh",
    "stt_hinh",
    "hinh_anh",
    "so_hinh",
    "ghi_ton",
}
IMAGE_TYPES = {
    "Bien_hieu",
    "Ke_trung_bay",
    "Tu_mat",
    "Thung_hang",
    "Loc_6_chai",
    "Chai_le",
    "Duoi_san",
    "Doi_pho",
    "Selfie_NV",
    "Khac",
    "Khong_doc_duoc",
}
DISPLAY_TYPES = {"Ke_trung_bay", "Tu_mat", "Thung_hang"}
OUTPUT_COLUMNS = [
    "source_row",
    "row_key",
    "ma_nv",
    "ten_nhan_vien",
    "ngay",
    "ma_kh",
    "ten_kh",
    "stt_hinh",
    "hinh_anh",
    "image_type",
    "has_khanh_hoa_product",
    "detected_brands",
    "visit_result",
    "display_result",
    "qa_note",
    "confidence",
    "evidence",
    "model",
    "graded_at_utc",
]


SYSTEM_PROMPT = """Bạn là bộ phân loại QA ảnh viếng thăm điểm bán tại Việt Nam.
Nhiệm vụ của bạn CHỈ là quan sát từng ảnh và trả về dữ liệu nhận diện; KHÔNG tự quyết định Đạt/Không đạt.

Phân loại image_type thành đúng MỘT giá trị:
- Bien_hieu: ảnh thể hiện rõ biển hiệu/bảng hiệu cửa hàng, tiệm tạp hóa, đại lý, siêu thị mini hoặc điểm bán. Nếu biển hiệu là nội dung chính thì dùng Bien_hieu kể cả khi ảnh cũng nhìn thấy sản phẩm.
- Ke_trung_bay: kệ hàng có hơn 6 chai/lon sản phẩm trưng bày.
- Tu_mat: tủ mát/tủ lạnh trưng bày có hơn 6 chai/lon.
- Thung_hang: các thùng carton hàng hóa được xếp chồng thành cụm/stack.
- Loc_6_chai: chủ yếu là một lốc 6 chai đơn lẻ.
- Chai_le: chủ yếu là chai/lon lẻ, không đủ điều kiện kệ/tủ/thùng.
- Duoi_san: sản phẩm chủ yếu đặt dưới sàn/nền.
- Doi_pho: ảnh đối phó, quá cận, che khuất, mờ, vô nghĩa hoặc không đủ thông tin để kiểm tra thực tế.
- Selfie_NV: ảnh nhân viên/selfie/người là chủ thể chính.
- Khac: ảnh khác nhưng vẫn đọc được nội dung.
- Khong_doc_duoc: ảnh hỏng, không tải được hoặc gần như không thể nhận diện.

Nhận diện sản phẩm Khánh Hòa thật NGHIÊM NGẶT:
- has_khanh_hoa_product=true chỉ khi nhìn thấy đủ rõ sản phẩm/thương hiệu Vikoda, Đảnh Thạnh hoặc Sumo; bao gồm chai/lon/bình/thùng Vikoda, Đảnh Thạnh hoặc Sumo.
- Không suy đoán từ màu sắc chung, kệ hàng, chai nước không rõ nhãn hoặc logo quá mờ.
- Nếu không chắc chắn, đặt false.
- detected_brands chỉ dùng các giá trị Vikoda, Đảnh Thạnh, Sumo và ngăn cách bằng dấu |; để trống nếu không nhận diện chắc chắn.

Trả về JSON object duy nhất dạng:
{"results":[{"idx":1,"image_type":"Bien_hieu","has_khanh_hoa_product":false,"detected_brands":"","confidence":0.92,"evidence":"Mô tả rất ngắn bằng tiếng Việt"}]}
Không thêm markdown, giải thích ngoài JSON hoặc trường khác.
"""


@dataclass(frozen=True)
class GraderConfig:
    source_csv_url: str
    output_path: Path
    model: str
    batch_size: int
    max_rows: int | None
    request_timeout: int
    max_retries: int
    stop_on_rate_limit: bool

    @classmethod
    def from_env(cls) -> "GraderConfig":
        batch_size = int(os.environ.get("GRADE_BATCH_SIZE", "20"))
        if batch_size < 1 or batch_size > 20:
            raise ValueError("GRADE_BATCH_SIZE must be between 1 and 20")
        max_rows_raw = os.environ.get("GRADE_MAX_ROWS", "").strip()
        max_rows = int(max_rows_raw) if max_rows_raw else None
        if max_rows is not None and max_rows < 1:
            raise ValueError("GRADE_MAX_ROWS must be >= 1")
        return cls(
            source_csv_url=os.environ.get("GRADE_SOURCE_CSV_URL", DEFAULT_SOURCE_CSV_URL),
            output_path=Path(os.environ.get("GRADE_OUTPUT_PATH", "results/vieng_tham_grading.csv")),
            model=os.environ.get("GRADE_MODEL", DEFAULT_MODEL),
            batch_size=batch_size,
            max_rows=max_rows,
            request_timeout=int(os.environ.get("GRADE_REQUEST_TIMEOUT", "180")),
            max_retries=int(os.environ.get("GRADE_MAX_RETRIES", "6")),
            stop_on_rate_limit=os.environ.get("GRADE_STOP_ON_RATE_LIMIT", "true").lower()
            not in {"0", "false", "no"},
        )


def row_key(row: dict[str, str]) -> str:
    material = "\x1f".join(
        [
            row.get("ma_nv", "").strip(),
            row.get("ngay", "").strip(),
            row.get("ma_kh", "").strip(),
            row.get("stt_hinh", "").strip(),
            row.get("hinh_anh", "").strip(),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def fetch_source_rows(url: str, timeout: int = 120) -> list[dict[str, str]]:
    response = requests.get(url, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").lower()
    text = response.text
    if "html" in content_type and "ma_nv" not in text[:5000]:
        raise RuntimeError(
            "Google Sheet CSV export returned HTML instead of CSV. "
            "Confirm the sheet remains accessible to anyone with the link."
        )
    reader = csv.DictReader(io.StringIO(text))
    columns = set(reader.fieldnames or [])
    missing = REQUIRED_SOURCE_COLUMNS - columns
    if missing:
        raise RuntimeError(f"Source CSV missing columns: {sorted(missing)}")
    rows = []
    for source_row, row in enumerate(reader, start=2):
        normalized = {key: (value or "").strip() for key, value in row.items()}
        if not normalized.get("hinh_anh"):
            continue
        normalized["source_row"] = str(source_row)
        normalized["row_key"] = row_key(normalized)
        rows.append(normalized)
    return rows


def read_existing(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return {
            row["row_key"]: row
            for row in reader
            if row.get("row_key") and row.get("image_type") in IMAGE_TYPES
        }


def write_results(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in OUTPUT_COLUMNS})
    temp.replace(path)


def clean_json_text(text: str) -> str:
    stripped = text.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def parse_model_payload(text: str, expected_count: int) -> list[dict[str, Any]]:
    payload = json.loads(clean_json_text(text))
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("Model response must be an object containing results[]")
    raw_results = payload["results"]
    if len(raw_results) != expected_count:
        raise ValueError(
            f"Model returned {len(raw_results)} results for {expected_count} images"
        )
    by_idx: dict[int, dict[str, Any]] = {}
    for item in raw_results:
        if not isinstance(item, dict):
            raise ValueError("Model result item must be an object")
        idx = int(item.get("idx", 0))
        if idx < 1 or idx > expected_count or idx in by_idx:
            raise ValueError(f"Invalid or duplicate idx={idx}")
        image_type = str(item.get("image_type", "")).strip()
        if image_type not in IMAGE_TYPES:
            raise ValueError(f"Unsupported image_type={image_type!r}")
        has_product = item.get("has_khanh_hoa_product")
        if not isinstance(has_product, bool):
            raise ValueError("has_khanh_hoa_product must be boolean")
        brands = str(item.get("detected_brands", "")).strip()
        allowed_brands = {"Vikoda", "Đảnh Thạnh", "Sumo"}
        if brands:
            normalized_brands = [brand.strip() for brand in brands.split("|") if brand.strip()]
            if not set(normalized_brands).issubset(allowed_brands):
                raise ValueError(f"Unsupported detected_brands={brands!r}")
            brands = "|".join(dict.fromkeys(normalized_brands))
        confidence = float(item.get("confidence", 0))
        confidence = min(max(confidence, 0.0), 1.0)
        evidence = str(item.get("evidence", "")).strip()[:500]
        by_idx[idx] = {
            "image_type": image_type,
            "has_khanh_hoa_product": has_product,
            "detected_brands": brands,
            "confidence": confidence,
            "evidence": evidence,
        }
    return [by_idx[idx] for idx in range(1, expected_count + 1)]


def apply_rules(classification: dict[str, Any]) -> dict[str, str]:
    image_type = classification["image_type"]
    has_product = bool(classification["has_khanh_hoa_product"])

    visit_result = "Đạt" if image_type == "Bien_hieu" else "Không đạt"
    display_result = (
        "Đạt" if image_type in DISPLAY_TYPES and has_product else "Không đạt"
    )

    qa_note = ""
    if image_type == "Bien_hieu" and has_product:
        qa_note = (
            "QA cần rà ảnh: ảnh đang nhận diện Biển hiệu nhưng đồng thời phát hiện "
            "sản phẩm Khánh Hòa. Kiểm tra thủ công xem sản phẩm có trưng bày đúng trên "
            "kệ/tủ mát hoặc thùng carton xếp chồng hay không; chưa tự nâng Chấm Trưng Bày."
        )
    elif image_type in DISPLAY_TYPES and not has_product:
        qa_note = (
            f"QA sửa: ảnh được phân loại {image_type} nhưng không nhận diện được sản phẩm "
            "Vikoda/Đảnh Thạnh/Sumo; chưa đủ căn cứ chấm Trưng Bày Đạt."
        )
    elif image_type == "Loc_6_chai":
        qa_note = (
            "QA sửa: lốc 6 chai đơn lẻ chưa thuộc quy cách Trưng Bày Đạt hiện tại; "
            "chỉ Đạt khi ảnh thể hiện rõ sản phẩm Khánh Hòa đặt trên kệ/tủ mát hoặc "
            "thùng carton xếp chồng đúng chuẩn."
        )
    elif image_type == "Chai_le":
        qa_note = "Không đạt Trưng Bày: ảnh chủ yếu là chai/lon lẻ."
    elif image_type == "Duoi_san":
        qa_note = "Không đạt Trưng Bày: sản phẩm chủ yếu đặt dưới sàn/nền."
    elif image_type == "Doi_pho":
        qa_note = "Không đạt: ảnh đối phó/không đủ thông tin kiểm tra."
    elif image_type == "Selfie_NV":
        qa_note = "Không đạt: ảnh nhân viên/selfie."
    elif image_type == "Khong_doc_duoc":
        qa_note = "QA cần rà ảnh: ảnh không đọc được hoặc không thể nhận diện đáng tin cậy."

    return {
        "visit_result": visit_result,
        "display_result": display_result,
        "qa_note": qa_note,
    }


def build_message_content(batch: list[dict[str, str]]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": SYSTEM_PROMPT}]
    for idx, row in enumerate(batch, start=1):
        content.append(
            {
                "type": "text",
                "text": (
                    f"IMAGE {idx}: source_row={row['source_row']}; ma_kh={row.get('ma_kh','')}; "
                    f"ten_kh={row.get('ten_kh','')}; stt_hinh={row.get('stt_hinh','')}"
                ),
            }
        )
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": row["hinh_anh"], "detail": "low"},
            }
        )
    content.append(
        {
            "type": "text",
            "text": f"Trả chính xác {len(batch)} phần tử trong results, idx từ 1 đến {len(batch)}.",
        }
    )
    return content


class GitHubModelsClient:
    def __init__(
        self,
        token: str,
        model: str,
        timeout: int = 180,
        max_retries: int = 6,
        session: requests.Session | None = None,
    ) -> None:
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2026-03-10",
            }
        )

    def classify(self, batch: list[dict[str, str]]) -> list[dict[str, Any]]:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": build_message_content(batch)}],
            "temperature": 0,
            "max_tokens": min(3500, 120 + 140 * len(batch)),
            "response_format": {"type": "json_object"},
        }
        retryable = {408, 429, 500, 502, 503, 504}
        response: requests.Response | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(
                    MODELS_ENDPOINT, json=body, timeout=self.timeout
                )
            except (requests.Timeout, requests.ConnectionError):
                if attempt >= self.max_retries:
                    raise
                time.sleep(min(3 * (2**attempt) + random.random(), 60))
                continue

            if response.status_code < 400:
                payload = response.json()
                text = payload["choices"][0]["message"]["content"]
                return parse_model_payload(text, len(batch))

            if response.status_code not in retryable or attempt >= self.max_retries:
                message = response.text[:1000]
                raise RuntimeError(
                    f"GitHub Models HTTP {response.status_code}: {message}"
                )

            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = min(max(float(retry_after), 1), 300)
                except ValueError:
                    delay = min(5 * (2**attempt), 120)
            else:
                delay = min(5 * (2**attempt) + random.random(), 120)
            time.sleep(delay)

        raise RuntimeError("Unreachable model retry loop")


def classify_with_recovery(
    client: GitHubModelsClient, batch: list[dict[str, str]]
) -> list[dict[str, Any]]:
    try:
        return client.classify(batch)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        if len(batch) == 1:
            raise RuntimeError(f"Invalid model response for one image: {exc}") from exc
        midpoint = len(batch) // 2
        left = classify_with_recovery(client, batch[:midpoint])
        right = classify_with_recovery(client, batch[midpoint:])
        return left + right


def build_output_row(
    source: dict[str, str], classification: dict[str, Any], model: str
) -> dict[str, Any]:
    rules = apply_rules(classification)
    return {
        "source_row": source["source_row"],
        "row_key": source["row_key"],
        "ma_nv": source.get("ma_nv", ""),
        "ten_nhan_vien": source.get("ten_nhan_vien", ""),
        "ngay": source.get("ngay", ""),
        "ma_kh": source.get("ma_kh", ""),
        "ten_kh": source.get("ten_kh", ""),
        "stt_hinh": source.get("stt_hinh", ""),
        "hinh_anh": source["hinh_anh"],
        "image_type": classification["image_type"],
        "has_khanh_hoa_product": "TRUE"
        if classification["has_khanh_hoa_product"]
        else "FALSE",
        "detected_brands": classification.get("detected_brands", ""),
        **rules,
        "confidence": f"{float(classification.get('confidence', 0)):.3f}",
        "evidence": classification.get("evidence", ""),
        "model": model,
        "graded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in [
            "http 429",
            "rate limit",
            "quota",
            "requests per day",
            "too many requests",
        ]
    )


def run(config: GraderConfig) -> int:
    source_rows = fetch_source_rows(config.source_csv_url)
    existing = read_existing(config.output_path)
    pending = [row for row in source_rows if row["row_key"] not in existing]
    if config.max_rows is not None:
        pending = pending[: config.max_rows]

    print(
        f"Source images={len(source_rows):,}; completed={len(existing):,}; "
        f"pending_this_run={len(pending):,}; batch_size={config.batch_size}"
    )
    if not pending:
        print("Nothing to grade.")
        return 0

    client = GitHubModelsClient(
        token=os.environ.get("GITHUB_TOKEN", ""),
        model=config.model,
        timeout=config.request_timeout,
        max_retries=config.max_retries,
    )

    result_by_key = dict(existing)
    completed_this_run = 0
    stopped_by_rate_limit = False
    for start in range(0, len(pending), config.batch_size):
        batch = pending[start : start + config.batch_size]
        try:
            classifications = classify_with_recovery(client, batch)
        except Exception as exc:
            if config.stop_on_rate_limit and is_rate_limit_error(exc):
                print(f"Rate limit reached; saving checkpoint: {exc}", file=sys.stderr)
                stopped_by_rate_limit = True
                break
            raise

        for source, classification in zip(batch, classifications, strict=True):
            result_by_key[source["row_key"]] = build_output_row(
                source, classification, config.model
            )
        completed_this_run += len(batch)

        ordered_results = [
            result_by_key[row["row_key"]]
            for row in source_rows
            if row["row_key"] in result_by_key
        ]
        write_results(config.output_path, ordered_results)
        print(
            f"Checkpoint: graded {completed_this_run:,}/{len(pending):,} in this run; "
            f"total={len(ordered_results):,}/{len(source_rows):,}"
        )

    ordered_results = [
        result_by_key[row["row_key"]]
        for row in source_rows
        if row["row_key"] in result_by_key
    ]
    write_results(config.output_path, ordered_results)

    summary = {
        "source_images": len(source_rows),
        "graded_images": len(ordered_results),
        "completed_this_run": completed_this_run,
        "remaining_images": len(source_rows) - len(ordered_results),
        "stopped_by_rate_limit": stopped_by_rate_limit,
        "model": config.model,
        "output": str(config.output_path),
    }
    summary_path = config.output_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def main() -> int:
    return run(GraderConfig.from_env())


if __name__ == "__main__":
    raise SystemExit(main())
