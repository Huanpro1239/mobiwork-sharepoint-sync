from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Mapping
from urllib.parse import parse_qs, unquote, urlparse

import pandas as pd


RECORD_ID_SCHEMA = "dms-v2.4"
RECORD_ID_FIELDS = ("ten_nhan_vien", "ngay", "ma_kh", "stt_hinh", "hinh_anh")


def _clean(value, default=""):
    if value is None:
        return default
    try:
        if bool(pd.isna(value)):
            return default
    except (TypeError, ValueError):
        pass
    return value


def _normalise_date(value) -> str:
    parsed = pd.to_datetime(_clean(value), errors="coerce")
    return str(_clean(value))[:10] if pd.isna(parsed) else parsed.strftime("%Y-%m-%d")


def stable_record_id(row: Mapping[str, object], duplicate_ordinal: int = 0) -> str:
    fields = []
    for key in RECORD_ID_FIELDS:
        value = _normalise_date(row.get(key)) if key == "ngay" else " ".join(str(_clean(row.get(key))).strip().split())
        fields.append(value)
    payload = {"schema": RECORD_ID_SCHEMA, "fields": fields, "duplicate_ordinal": int(duplicate_ordinal)}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def assign_record_ids(rows: list[Mapping[str, object]]) -> list[str]:
    seen: dict[str, int] = {}
    output: list[str] = []
    for row in rows:
        base = stable_record_id(row)
        ordinal = seen.get(base, 0)
        seen[base] = ordinal + 1
        output.append(base if ordinal == 0 else stable_record_id(row, ordinal))
    return output


def _filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    nested = parse_qs(parsed.query).get("url", ())
    source = unquote(nested[0] if nested else parsed.path)
    return PurePosixPath(source).name


def score_to_payload(score_result) -> dict[str, object]:
    decision = score_result.decision
    classification = score_result.classification
    scores = classification.scores
    neighbors = classification.neighbors
    evidence = score_result.evidence
    evidence_text = "; ".join(
        (
            f"signboard={int(bool(evidence.has_signboard))}",
            f"brand_keyword={int(bool(evidence.has_brand_keyword))}",
            f"bottle_or_pack={int(bool(evidence.has_bottle_or_pack))}",
            f"face_audit={int(bool(evidence.has_face))}",
        )
    )
    warnings = tuple(getattr(score_result, "audit_warnings", ()))
    if warnings:
        evidence_text += "; audit_warning=" + " | ".join(warnings)
    nearest = "; ".join(
        f"{n.relative_path} [{n.effective_subcategory}] ({n.similarity:.3f})" for n in neighbors[:3]
    )
    confidence = None if decision.label == "Can_duyet" else decision.score if decision.score > 0 else scores.pass_probability
    return {
        "Phân Loại AI": decision.label,
        "Độ Tin Cậy AI": round(float(confidence), 6) if confidence is not None else None,
        "Căn Cứ Nhận Diện": f"{decision.status}: {' | '.join(decision.reasons)} | {evidence_text}",
        "Nội Dung Chữ OCR": str(score_result.ocr_text or ""),
        "Trạng Thái Quyết Định": decision.status,
        "Loại Cảnh": decision.scene,
        "Điểm Scene": round(float(scores.sign_probability), 6),
        "Điểm Pass": round(float(scores.pass_probability), 6),
        "Điểm Fraud": round(float(scores.fraud_probability), 6),
        "Độ Tương Đồng Mẫu": round(float(scores.reference_similarity), 6),
        "3 Tham Chiếu Gần Nhất": nearest,
        "Bằng Chứng Detector": evidence_text,
        "Quality Gate": bool(classification.quality_gate_passed),
    }


def technical_failure_payload(reason: str) -> dict[str, object]:
    return {
        "Phân Loại AI": "Khong_the_cham",
        "Độ Tin Cậy AI": None,
        "Căn Cứ Nhận Diện": f"Lỗi kỹ thuật: {reason} [TECHNICAL_FAILURE]",
        "Nội Dung Chữ OCR": "",
        "Trạng Thái Quyết Định": "TECHNICAL_FAILURE",
        "Loại Cảnh": "Unknown",
        "Điểm Scene": None,
        "Điểm Pass": None,
        "Điểm Fraud": None,
        "Độ Tương Đồng Mẫu": None,
        "3 Tham Chiếu Gần Nhất": "",
        "Bằng Chứng Detector": "",
        "Quality Gate": "N/A",
    }


def build_audit_record(
    row: Mapping[str, object],
    record_id: str,
    pipeline_signature: str,
    image_sha256: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    url = str(_clean(row.get("hinh_anh"))).strip()
    record = {
        "record_id": record_id,
        "pipeline_signature": pipeline_signature,
        "ten_nhan_vien": _clean(row.get("ten_nhan_vien")),
        "ngay": _normalise_date(row.get("ngay")),
        "ma_kh": _clean(row.get("ma_kh")),
        "ten_kh": _clean(row.get("ten_kh")),
        "stt_hinh": _clean(row.get("stt_hinh")),
        "hinh_anh": url,
        "ghi_chu": _clean(row.get("ghi_chu")),
        "Tên File": _filename_from_url(url),
        "image_sha256": image_sha256,
    }
    record.update(dict(payload))
    record["score_payload_json"] = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return record
