"""YOLO-World evidence detector used by the scoring policy."""

from scoring.config import YOLO_CLASSES, YOLO_CONFIDENCE, YOLO_WEIGHTS


_POLICY_EVIDENCE_CLASSES = frozenset({"bottle", "pack of bottles", "signboard"})
EVIDENCE_CLASSES = tuple(
    class_name
    for class_name in YOLO_CLASSES
    if class_name in _POLICY_EVIDENCE_CLASSES
)
_DETECTION_BUCKET = {
    "bottle": "bottles",
    "pack of bottles": "packs",
    "signboard": "signboards",
}


def _empty_detections():
    return {bucket: [] for bucket in _DETECTION_BUCKET.values()}


def _group_detections(class_ids, coordinates):
    detections = _empty_detections()
    for class_value, box in zip(class_ids, coordinates, strict=True):
        class_id = int(class_value)
        if not 0 <= class_id < len(EVIDENCE_CLASSES):
            continue
        class_name = EVIDENCE_CLASSES[class_id]
        detections[_DETECTION_BUCKET[class_name]].append(box)
    return detections


class YOLODetector:
    def __init__(self):
        from ultralytics import YOLOWorld

        self.model = YOLOWorld(str(YOLO_WEIGHTS))
        self.model.set_classes(EVIDENCE_CLASSES)

    def detect(self, image_bgr):
        """Detect policy evidence from an OpenCV BGR image."""

        results = self.model(image_bgr, conf=YOLO_CONFIDENCE, verbose=False)[0]
        boxes = results.boxes
        if boxes is None or len(boxes) == 0:
            return _empty_detections()

        class_ids = boxes.cls.detach().cpu().numpy()
        coordinates = boxes.xyxy.detach().cpu().numpy()
        return _group_detections(class_ids, coordinates)
