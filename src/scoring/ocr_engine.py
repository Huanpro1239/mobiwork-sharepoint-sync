import cv2
import easyocr
import torch
import unicodedata

from scoring.config import BRAND_OCR_KEYWORDS, STORE_OCR_KEYWORDS


def _normalize_text(text):
    decomposed = unicodedata.normalize("NFKD", str(text).casefold())
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(without_marks.replace("đ", "d").split())


NORMALIZED_BRAND_KEYWORDS = tuple(_normalize_text(keyword) for keyword in BRAND_OCR_KEYWORDS)
NORMALIZED_STORE_KEYWORDS = tuple(_normalize_text(keyword) for keyword in STORE_OCR_KEYWORDS)


class TargetedOCREngine:
    def __init__(self):
        use_gpu = torch.cuda.is_available()
        self.reader = easyocr.Reader(["vi", "en"], gpu=use_gpu)

    def extract_text(self, image_bgr, signboard_boxes=None):
        """Read signboard-focused text while keeping CPU work bounded."""
        texts = []

        if signboard_boxes is not None and len(signboard_boxes) > 0:
            height, width = image_bgr.shape[:2]
            for box in signboard_boxes:
                x1, y1, x2, y2 = map(int, box)
                crop = image_bgr[
                    max(0, y1) : min(height, y2),
                    max(0, x1) : min(width, x2),
                ]
                if crop.size > 0:
                    texts.append(self._ocr_crop(crop))
        else:
            height = image_bgr.shape[0]
            upper_crop = image_bgr[0 : max(1, int(height * 0.4)), :]
            texts.append(self._ocr_crop(upper_crop))

        return " ".join(text for text in texts if text).strip()

    def _ocr_crop(self, crop):
        height, width, _ = crop.shape
        if height < 10 or width < 10:
            return ""

        if width > 800:
            scale = 800.0 / width
            new_height = max(1, int(height * scale))
            crop = cv2.resize(crop, (800, new_height), interpolation=cv2.INTER_AREA)

        results = self.reader.readtext(crop)
        return " ".join(result[1] for result in results)

    @staticmethod
    def _contains(text, keywords):
        if not text:
            return False
        normalized_text = _normalize_text(text)
        return any(keyword in normalized_text for keyword in keywords)

    def has_brand_keyword(self, text):
        return self._contains(text, NORMALIZED_BRAND_KEYWORDS)

    def has_store_keyword(self, text):
        return self._contains(text, NORMALIZED_STORE_KEYWORDS)

    def has_brand_or_store_keyword(self, text):
        """Backward-compatible general OCR evidence helper."""
        return self.has_brand_keyword(text) or self.has_store_keyword(text)

    @staticmethod
    def _normalize_text(text):
        return _normalize_text(text)
