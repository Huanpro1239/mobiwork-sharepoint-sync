from __future__ import annotations

import csv
import io
import os
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageOps


def load_rows(path: Path, limit: int = 20) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))[:limit]


def fetch_image(url: str) -> Image.Image:
    response = requests.get(
        url,
        timeout=45,
        headers={"User-Agent": "Mozilla/5.0 DMS-QA/1.0"},
    )
    response.raise_for_status()
    return Image.open(io.BytesIO(response.content)).convert("RGB")


def fit_image(image: Image.Image, width: int, height: int) -> Image.Image:
    return ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS)


def main() -> int:
    input_path = Path(os.environ.get("GRADE_OUTPUT_PATH", "results/vieng_tham_grading.csv"))
    output_path = Path(os.environ.get("GRADE_CONTACT_SHEET", "results/smoke_contact_sheet.jpg"))
    rows = load_rows(input_path, int(os.environ.get("GRADE_CONTACT_LIMIT", "20")))
    if not rows:
        return 0

    cols = 4
    tile_w = 360
    image_h = 270
    label_h = 92
    tile_h = image_h + label_h
    grid_rows = (len(rows) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * tile_w, grid_rows * tile_h), "white")
    draw = ImageDraw.Draw(canvas)

    for idx, row in enumerate(rows):
        x = (idx % cols) * tile_w
        y = (idx // cols) * tile_h
        try:
            image = fit_image(fetch_image(row["hinh_anh"]), tile_w, image_h)
            canvas.paste(image, (x, y))
        except Exception as exc:
            draw.rectangle((x, y, x + tile_w - 1, y + image_h - 1), outline="black")
            draw.text((x + 8, y + 8), f"DOWNLOAD ERROR: {type(exc).__name__}", fill="black")

        brand = row.get("detected_brands", "") or "-"
        label = (
            f"row {row.get('source_row','')} | {row.get('image_type','')} | conf {row.get('confidence','')}\n"
            f"visit={row.get('visit_result','')} | display={row.get('display_result','')}\n"
            f"brand={brand} | KH={row.get('has_khanh_hoa_product','')}\n"
            f"{row.get('ten_kh','')[:42]}"
        )
        draw.rectangle((x, y + image_h, x + tile_w - 1, y + tile_h - 1), fill="white", outline="black")
        draw.multiline_text((x + 6, y + image_h + 5), label, fill="black", spacing=3)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, quality=88, optimize=True)
    print(f"Wrote contact sheet: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
