"""Local plate reader (fast-alpr: ONNX on CPU). A free second opinion next to the extractor."""

import re
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
    def read(self, image: Image.Image) -> list[PlateRead]:
        """Readable plates in the photo, most confident detection first."""
        ...


def normalize_plate(text: str | None) -> str | None:
    cleaned = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
    return cleaned or None


def readable(plates: list[PlateRead]) -> list[PlateRead]:
    """Drop detections the OCR couldn't read (the detector also fires on things like tire tread)
    and rank the rest by detection confidence. Box size is not a signal: street photos often
    show a small, distant plate next to a large false detection."""
    kept = [plate for plate in plates if normalize_plate(plate.text)]
    return sorted(kept, key=lambda plate: plate.detection_confidence or 0.0, reverse=True)


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

    def read(self, image: Image.Image) -> list[PlateRead]:
        frame = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()  # RGB -> BGR
        plates = []
        for result in self._model().predict(frame):
            box = result.detection.bounding_box
            plates.append(PlateRead(
                text=result.ocr.text if result.ocr else None,
                ocr_confidence=_mean(result.ocr.confidence) if result.ocr else None,
                detection_confidence=_mean(result.detection.confidence),
                box=(int(box.x1), int(box.y1), int(box.x2), int(box.y2)),
            ))
        return readable(plates)
