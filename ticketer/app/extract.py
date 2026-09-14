"""Vehicle extraction: photo in, plate / color / make / model out.

`Extractor` is provider-neutral; `ClaudeExtractor` is the default. Before changing the default
model, run the candidate through the eval harness and compare the scores: it must not add
confident-but-wrong plates.
"""

import base64
import io
from dataclasses import dataclass
from typing import Literal, Protocol

import anthropic
from PIL import Image
from pydantic import BaseModel

# Same prompt the eval harness scored.
SYSTEM = """You extract vehicle details from phone photos for a residential parking \
enforcement report in Sacramento, California. Each photo has one parked subject vehicle; \
ignore other vehicles in the background.

Field rules:
- plate_text: the subject vehicle's plate characters, uppercase, no spaces or dashes. If some \
characters are hard to read, give your best reading and set plate_confidence to "low". If no \
plate is visible, use null.
- plate_state: two-letter state code if readable on the plate, otherwise null.
- color: one common color name (white, black, silver, grey, red, blue, ...).
- make and model: manufacturer and model name, without year or trim. Use null for anything \
you cannot determine."""


class VehicleReport(BaseModel):
    plate_text: str | None
    plate_state: str | None
    plate_confidence: Literal["high", "medium", "low"]
    color: str
    make: str | None
    model: str | None
    make_model_confidence: Literal["high", "medium", "low"]
    notes: str


@dataclass(frozen=True)
class Extraction:
    report: VehicleReport
    model: str
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class ExtractionError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class Extractor(Protocol):
    def extract(self, image: Image.Image) -> Extraction: ...


# USD per million tokens (input, output) and the longest image edge the model uses natively.
CLAUDE_MODELS = {
    "claude-sonnet-5": {"price": (2.00, 10.00), "long_edge": 2576},
    "claude-opus-5": {"price": (5.00, 25.00), "long_edge": 2576},
}
DEFAULT_LONG_EDGE = 1568


def jpeg_base64(image: Image.Image, long_edge: int) -> str:
    """Downscale to the model's native size and re-encode, which also drops EXIF (incl. GPS)."""
    scale = min(1.0, long_edge / max(image.size))
    if scale < 1.0:
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=90)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


class ClaudeExtractor:
    def __init__(self, model: str, api_key: str):
        self.model = model
        self.spec = CLAUDE_MODELS.get(model, {})
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=120.0)

    def extract(self, image: Image.Image) -> Extraction:
        data = jpeg_base64(image, self.spec.get("long_edge", DEFAULT_LONG_EDGE))
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                        {"type": "text", "text": "Extract the vehicle details."},
                    ],
                }],
                output_format=VehicleReport,
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise ExtractionError(f"Anthropic API key rejected: {e.message}", retryable=False) from e
        except (anthropic.BadRequestError, anthropic.NotFoundError) as e:
            raise ExtractionError(f"Anthropic API rejected the request: {e.message}", retryable=False) from e
        except anthropic.RateLimitError as e:
            raise ExtractionError("Anthropic API rate limit reached", retryable=True) from e
        except anthropic.APIStatusError as e:
            raise ExtractionError(f"Anthropic API error {e.status_code}: {e.message}",
                                  retryable=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:  # includes timeouts
            raise ExtractionError(f"Can't reach the Anthropic API: {e}", retryable=True) from e

        if response.stop_reason == "refusal":
            raise ExtractionError("The model declined to describe this photo", retryable=False)
        if response.parsed_output is None:
            raise ExtractionError(f"No structured result (stop reason: {response.stop_reason})", retryable=False)

        usage = response.usage
        cost = None
        if "price" in self.spec:
            in_price, out_price = self.spec["price"]
            cost = (usage.input_tokens * in_price + usage.output_tokens * out_price) / 1e6
        return Extraction(report=response.parsed_output, model=self.model, request_id=response._request_id,
                          input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, cost_usd=cost)
