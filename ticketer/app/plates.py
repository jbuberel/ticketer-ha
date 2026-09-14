"""Local plate reader (fast-alpr: ONNX on CPU). A free second opinion next to the extractor."""

import threading
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from PIL import Image

# Keep in sync with the model download in the Dockerfile.
DETECTOR_MODEL = "yolo-v9-t-384-license-plate-end2end"
OCR_MODEL = "cct-s-v2-global-model"


@dataclass(frozen=True)
class PlateRead:
    text: str | None
    ocr_confidence: float | None
    detection_confidence: float | None
    box: tuple[int, int, int, int]  # x1, y1, x2, y2 in the upright image


class PlateReader(Protocol):
    def read(self, image: Image.Image) -> PlateRead | None: ...


def _mean(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple, np.ndarray)):
        return float(np.mean(value)) if len(value) else None
    return float(value)


class FastAlprReader:
    def __init__(self):
        self._alpr = None
        self._lock = threading.Lock()

    def _model(self):
        with self._lock:
            if self._alpr is None:
                from fast_alpr import ALPR  # heavy import; load on first use

                self._alpr = ALPR(detector_model=DETECTOR_MODEL, ocr_model=OCR_MODEL)
            return self._alpr

    def read(self, image: Image.Image) -> PlateRead | None:
        """The largest detected plate, which is normally the subject vehicle's."""
        frame = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()  # RGB -> BGR
        results = self._model().predict(frame)
        if not results:
            return None

        def area(result) -> float:
            box = result.detection.bounding_box
            return (box.x2 - box.x1) * (box.y2 - box.y1)

        best = max(results, key=area)
        box = best.detection.bounding_box
        return PlateRead(
            text=best.ocr.text if best.ocr else None,
            ocr_confidence=_mean(best.ocr.confidence) if best.ocr else None,
            detection_confidence=_mean(best.detection.confidence),
            box=(int(box.x1), int(box.y1), int(box.x2), int(box.y2)),
        )
