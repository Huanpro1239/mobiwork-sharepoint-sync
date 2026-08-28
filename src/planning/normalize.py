from __future__ import annotations

import math
import re
import unicodedata
from typing import Any


SPACE_RE = re.compile(r"\s+")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ").strip()
    return SPACE_RE.sub(" ", text)


def remove_vietnamese_accents(value: Any) -> str:
    text = clean_text(value).replace("đ", "d").replace("Đ", "D")
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )


def normalize_compare_text(value: Any) -> str:
    text = remove_vietnamese_accents(value).upper()
    return re.sub(r"[^A-Z0-9]+", "", text)


def normalize_channel(value: Any) -> str:
    text = normalize_compare_text(value)
    if text in {"KA", "MT", "KAMT"}:
        return "KA/MT" if text == "KAMT" else text
    return text


def normalize_code(value: Any, mode: str = "none") -> str:
    code = clean_text(value).replace(",", "")
    if not code:
        return ""
    try:
        number = float(code)
        if math.isfinite(number) and number.is_integer():
            code = str(int(number))
    except (TypeError, ValueError):
        pass
    if mode == "1to2" and code.startswith("1"):
        return "2" + code[1:]
    if mode == "2to1" and code.startswith("2"):
        return "1" + code[1:]
    if mode not in {"none", "1to2", "2to1"}:
        raise ValueError(f"Unsupported code mode: {mode}")
    return code


def to_number(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        try:
            if math.isnan(float(value)):
                return default
        except (TypeError, ValueError):
            return default
        return float(value)
    text = clean_text(value).replace(" ", "")
    if not text:
        return default
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        right = text.rsplit(",", 1)[1]
        text = text.replace(",", ".") if len(right) <= 3 else text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return default
