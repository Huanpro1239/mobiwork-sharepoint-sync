import cv2
import easyocr
import torch
import unicodedata
from scoring.config import OCR_KEYWORDS


def _normalize_text(text):
    decomposed = unicodedata.normalize("NFKD", str(text).casefold())
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(without_marks.replace("đ", "d").split())


NORMALIZED_OCR_KEYWORDS = tuple(_normalize_text(keyword) for keyword in OCR_KEYWORDS)


class TargetedOCREngine:
    def __init__(self):
        use_gpu = torch.cuda.is_available()
        self.reader = easyocr.Reader(['vi', 'en'], gpu=use_gpu)

    def extract_text(self, image_bgr, signboard_boxes=None):
        """
        Quét chữ thông minh: Nếu có box biển hiệu thì crop box, nếu không chỉ quét 40% góc trên của ảnh
        và resize ảnh vừa phải để xử lý siêu tốc trên CPU.
        """
        texts = []

        if signboard_boxes is not None and len(signboard_boxes) > 0:
            height, width = image_bgr.shape[:2]
            for b in signboard_boxes:
                x1, y1, x2, y2 = map(int, b)
                crop = image_bgr[
                    max(0, y1) : min(height, y2),
                    max(0, x1) : min(width, x2),
                ]
                if crop.size > 0:
                    texts.append(self._ocr_crop(crop))
        else:
            # Crop 40% phía trên của ảnh nơi thường đặt biển hiệu
            h = image_bgr.shape[0]
            upper_crop = image_bgr[0 : max(1, int(h * 0.4)), :]
            texts.append(self._ocr_crop(upper_crop))

        full_text = " ".join([t for t in texts if t]).strip()
        return full_text

    def _ocr_crop(self, crop):
        h, w, _ = crop.shape
        if h < 10 or w < 10:
            return ""
        
        # Resize nếu ảnh quá lớn để OCR chạy trong 0.1-0.2s
        if w > 800:
            scale = 800.0 / w
            new_h = int(h * scale)
            crop = cv2.resize(crop, (800, new_h), interpolation=cv2.INTER_AREA)

        results = self.reader.readtext(crop)
        return " ".join([res[1] for res in results])

    def has_brand_or_store_keyword(self, text):
        if not text:
            return False
        normalized_text = self._normalize_text(text)
        return any(keyword in normalized_text for keyword in NORMALIZED_OCR_KEYWORDS)

    @staticmethod
    def _normalize_text(text):
        return _normalize_text(text)
