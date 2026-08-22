from __future__ import annotations

import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pytesseract
import requests
import torch
from PIL import Image
from transformers import CLIPModel, CLIPProcessor

from src.grade_visit_images import (
    GraderConfig,
    build_output_row,
    fetch_source_rows,
    read_existing,
    write_results,
)


TYPE_PROMPTS: dict[str, list[str]] = {
    "Bien_hieu": [
        "front of a small Vietnamese shop with a clearly visible store signboard",
        "Vietnamese grocery store storefront with a large shop sign or name board",
    ],
    "Ke_trung_bay": [
        "many beverage bottles or cans arranged on retail shelves",
        "more than six drink bottles neatly displayed on a shop shelf",
    ],
    "Tu_mat": [
        "many drink bottles inside a glass door refrigerator display cooler",
        "beverages arranged inside a refrigerated display cabinet",
    ],
    "Thung_hang": [
        "stacked cardboard beverage cartons or drink boxes in a shop",
        "a pile of beverage cartons stacked on top of each other",
    ],
    "Loc_6_chai": [
        "one shrink wrapped six pack of beverage bottles",
        "a single pack containing six drink bottles",
    ],
    "Chai_le": [
        "one or a few loose beverage bottles not arranged on a shelf",
        "a small number of individual drink bottles",
    ],
    "Duoi_san": [
        "beverage bottles or cartons placed directly on the floor",
        "drinks stored on the ground or shop floor",
    ],
    "Doi_pho": [
        "a blurry badly framed close up photo with little useful retail information",
        "an obstructed or meaningless compliance photo with no clear retail scene",
    ],
    "Selfie_NV": [
        "a selfie photo of a salesperson or employee",
        "a person taking a selfie as the main subject of the photo",
    ],
    "Khac": [
        "an ordinary photo that is not a storefront sign, retail shelf, cooler, carton stack, bottle pack or selfie",
        "other miscellaneous retail scene",
    ],
}

PRODUCT_VISIBILITY_PROMPTS = [
    "the photo clearly shows beverage bottles, cans or beverage cartons as visible products",
    "the photo is mainly a storefront sign and does not clearly show beverage products",
]


class LocalVisionClassifier:
    def __init__(self, model_name: str = "openai/clip-vit-base-patch32") -> None:
        self.model_name = model_name
        self.device = torch.device("cpu")
        self.processor = CLIPProcessor.from_pretrained(model_name)
        self.model = CLIPModel.from_pretrained(model_name)
        self.model.eval()
        self.model.to(self.device)

        self.type_names = list(TYPE_PROMPTS)
        self.type_prompt_texts: list[str] = []
        self.prompt_to_type: list[int] = []
        for type_index, image_type in enumerate(self.type_names):
            for prompt in TYPE_PROMPTS[image_type]:
                self.type_prompt_texts.append(prompt)
                self.prompt_to_type.append(type_index)

        with torch.inference_mode():
            type_text_inputs = self.processor(
                text=self.type_prompt_texts,
                return_tensors="pt",
                padding=True,
                truncation=True,
            )
            type_features = self.model.get_text_features(**type_text_inputs)
            self.type_text_features = torch.nn.functional.normalize(type_features, dim=-1)

            product_text_inputs = self.processor(
                text=PRODUCT_VISIBILITY_PROMPTS,
                return_tensors="pt",
                padding=True,
                truncation=True,
            )
            product_features = self.model.get_text_features(**product_text_inputs)
            self.product_text_features = torch.nn.functional.normalize(product_features, dim=-1)

    def classify_images(self, images: list[Image.Image]) -> list[dict[str, Any]]:
        if not images:
            return []
        with torch.inference_mode():
            image_inputs = self.processor(images=images, return_tensors="pt")
            image_features = self.model.get_image_features(**image_inputs)
            image_features = torch.nn.functional.normalize(image_features, dim=-1)

            prompt_logits = 100.0 * image_features @ self.type_text_features.T
            class_logits = torch.full(
                (len(images), len(self.type_names)),
                -1e9,
                dtype=prompt_logits.dtype,
            )
            for class_index in range(len(self.type_names)):
                prompt_indices = [
                    idx
                    for idx, mapped_index in enumerate(self.prompt_to_type)
                    if mapped_index == class_index
                ]
                class_logits[:, class_index] = prompt_logits[:, prompt_indices].mean(dim=1)

            class_probs = torch.softmax(class_logits, dim=1)
            product_logits = 100.0 * image_features @ self.product_text_features.T
            product_probs = torch.softmax(product_logits, dim=1)

        outputs: list[dict[str, Any]] = []
        for row_index in range(len(images)):
            confidence, type_index = torch.max(class_probs[row_index], dim=0)
            image_type = self.type_names[int(type_index)]
            outputs.append(
                {
                    "image_type": image_type,
                    "confidence": float(confidence),
                    "product_visibility": float(product_probs[row_index, 0]),
                }
            )
        return outputs


def _normalize_ocr_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^A-Z0-9]+", " ", ascii_text.upper()).strip()


def detect_brands(image: Image.Image) -> tuple[list[str], str]:
    ocr_image = image.copy()
    ocr_image.thumbnail((1400, 1400))
    text = pytesseract.image_to_string(ocr_image, config="--psm 11")
    normalized = _normalize_ocr_text(text)
    collapsed = normalized.replace(" ", "")

    brands: list[str] = []
    if "VIKODA" in collapsed or "VIK0DA" in collapsed:
        brands.append("Vikoda")
    if "DANHTHANH" in collapsed or "DANHTHANH" in normalized.replace(" ", ""):
        brands.append("Đảnh Thạnh")
    if re.search(r"(?:^| )SUMO(?: |$)", normalized) or "SUMO" in collapsed:
        brands.append("Sumo")

    return brands, normalized[:300]


def download_image(url: str, timeout: int = 45) -> Image.Image:
    response = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 DMS-QA/1.0"},
    )
    response.raise_for_status()
    from io import BytesIO

    image = Image.open(BytesIO(response.content)).convert("RGB")
    return image


def download_batch(rows: list[dict[str, str]], workers: int = 12) -> list[Image.Image | None]:
    images: list[Image.Image | None] = [None] * len(rows)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(download_image, row["hinh_anh"]): index
            for index, row in enumerate(rows)
        }
        for future in as_completed(future_map):
            index = future_map[future]
            try:
                images[index] = future.result()
            except Exception as exc:
                print(f"Image download failed row={rows[index]['source_row']}: {exc}")
    return images


def enrich_with_brand_detection(
    images: list[Image.Image | None], classifications: list[dict[str, Any]]
) -> None:
    candidate_types = {
        "Bien_hieu",
        "Ke_trung_bay",
        "Tu_mat",
        "Thung_hang",
        "Loc_6_chai",
        "Chai_le",
        "Duoi_san",
    }
    candidates = [
        index
        for index, (image, classification) in enumerate(zip(images, classifications, strict=True))
        if image is not None and classification["image_type"] in candidate_types
    ]
    if not candidates:
        return

    with ThreadPoolExecutor(max_workers=4) as executor:
        future_map = {
            executor.submit(detect_brands, images[index]): index
            for index in candidates
            if images[index] is not None
        }
        for future in as_completed(future_map):
            index = future_map[future]
            try:
                brands, ocr_text = future.result()
            except Exception as exc:
                brands, ocr_text = [], f"OCR error: {exc}"

            classification = classifications[index]
            image_type = classification["image_type"]
            product_visibility = float(classification.get("product_visibility", 0.0))
            if image_type == "Bien_hieu":
                has_product = bool(brands) and product_visibility >= 0.55
            else:
                has_product = bool(brands)

            classification["detected_brands"] = "|".join(brands)
            classification["has_khanh_hoa_product"] = has_product
            evidence_parts = [
                f"CLIP={image_type} ({classification['confidence']:.2f})",
                f"product_visibility={product_visibility:.2f}",
            ]
            if brands:
                evidence_parts.append("OCR brand=" + "|".join(brands))
            elif ocr_text:
                evidence_parts.append("OCR không thấy Vikoda/Đảnh Thạnh/Sumo")
            classification["evidence"] = "; ".join(evidence_parts)


def classify_batch(
    classifier: LocalVisionClassifier,
    rows: list[dict[str, str]],
    download_workers: int,
) -> list[dict[str, Any]]:
    images = download_batch(rows, workers=download_workers)
    valid_indices = [index for index, image in enumerate(images) if image is not None]
    valid_images = [images[index] for index in valid_indices if images[index] is not None]

    classifications: list[dict[str, Any]] = [
        {
            "image_type": "Khong_doc_duoc",
            "has_khanh_hoa_product": False,
            "detected_brands": "",
            "confidence": 0.0,
            "product_visibility": 0.0,
            "evidence": "Không tải được ảnh từ URL MobiWork.",
        }
        for _ in rows
    ]

    if valid_images:
        valid_results = classifier.classify_images(valid_images)  # type: ignore[arg-type]
        for index, result in zip(valid_indices, valid_results, strict=True):
            result.setdefault("has_khanh_hoa_product", False)
            result.setdefault("detected_brands", "")
            result.setdefault(
                "evidence",
                f"CLIP={result['image_type']} ({result['confidence']:.2f})",
            )
            classifications[index] = result

    enrich_with_brand_detection(images, classifications)
    return classifications


def main() -> int:
    config = GraderConfig.from_env()
    local_model = os.environ.get("GRADE_LOCAL_MODEL", "openai/clip-vit-base-patch32")
    batch_size = int(os.environ.get("GRADE_LOCAL_BATCH_SIZE", "32"))
    download_workers = int(os.environ.get("GRADE_DOWNLOAD_WORKERS", "12"))

    source_rows = fetch_source_rows(config.source_csv_url)
    existing = read_existing(config.output_path)
    pending = [row for row in source_rows if row["row_key"] not in existing]
    if config.max_rows is not None:
        pending = pending[: config.max_rows]

    print(
        f"Source images={len(source_rows):,}; completed={len(existing):,}; "
        f"pending_this_run={len(pending):,}; local_batch_size={batch_size}"
    )
    if not pending:
        return 0

    classifier = LocalVisionClassifier(local_model)
    result_by_key = dict(existing)
    completed_this_run = 0

    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        classifications = classify_batch(classifier, batch, download_workers)
        for source, classification in zip(batch, classifications, strict=True):
            result_by_key[source["row_key"]] = build_output_row(
                source,
                classification,
                model=f"local:{local_model}+tesseract",
            )
        completed_this_run += len(batch)

        ordered_results = [
            result_by_key[row["row_key"]]
            for row in source_rows
            if row["row_key"] in result_by_key
        ]
        write_results(config.output_path, ordered_results)
        print(
            f"Checkpoint: {completed_this_run:,}/{len(pending):,} this run; "
            f"total={len(ordered_results):,}/{len(source_rows):,}"
        )

    summary = {
        "source_images": len(source_rows),
        "graded_images": len(result_by_key),
        "completed_this_run": completed_this_run,
        "remaining_images": len(source_rows) - len(result_by_key),
        "engine": f"local:{local_model}+tesseract",
        "output": str(config.output_path),
    }
    config.output_path.with_suffix(".summary.json").write_text(
        __import__("json").dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
