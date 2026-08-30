import mediapipe as mp

from scoring.config import FACE_CONFIDENCE


class FaceDetector:
    def __init__(self):
        self.mp_face_detection = mp.solutions.face_detection
        self.detector = self.mp_face_detection.FaceDetection(
            model_selection=1,
            min_detection_confidence=FACE_CONFIDENCE,
        )

    def has_face(self, image_rgb):
        results = self.detector.process(image_rgb)
        return bool(results.detections)
