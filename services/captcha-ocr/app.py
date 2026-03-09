from __future__ import annotations

import base64
import io
import re
from typing import Iterable

import cairosvg
import pytesseract
from fastapi import FastAPI
from PIL import Image, ImageFilter, ImageOps
from pydantic import BaseModel

try:
    import ddddocr  # type: ignore
except Exception:
    ddddocr = None


app = FastAPI(title="Subtitle Captcha OCR", version="0.1.0")


class OCRRequest(BaseModel):
    image_base64: str
    content_type: str | None = None
    provider: str | None = None
    domain: str | None = None


class OCRResponse(BaseModel):
    code: str
    confidence: float
    engine: str = "none"


def _normalize_code(text: str, provider: str | None = None) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "", text or "").strip()
    provider_name = str(provider or "").strip().lower()
    if provider_name in {"subhd", "subhdtw"}:
        return normalized[:4] if len(normalized) >= 4 else ""
    return normalized[:12]


def _is_svg(image_bytes: bytes) -> bool:
    head = image_bytes[:256].lower()
    return b"<svg" in head or b"<?xml" in head


def _decode_svg_hint(image_bytes: bytes) -> tuple[str, float] | None:
    if not _is_svg(image_bytes):
        return None
    try:
        text = image_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return None
    # Priority: explicit <text> nodes, fallback to any short alnum token.
    nodes = re.findall(r"<text[^>]*>(.*?)</text>", text, flags=re.IGNORECASE | re.DOTALL)
    for raw in nodes:
        code = _normalize_code(raw)
        if len(code) >= 3:
            return code, 0.99
    return None


def _svg_bytes_to_png(image_bytes: bytes) -> bytes | None:
    try:
        return cairosvg.svg2png(bytestring=image_bytes, output_width=600, output_height=200)
    except Exception:
        return None


_ddddocr_beta_instance = None


def _get_ddddocr_beta():
    global _ddddocr_beta_instance
    if ddddocr is None:
        return None
    if _ddddocr_beta_instance is None:
        try:
            _ddddocr_beta_instance = ddddocr.DdddOcr(show_ad=False, beta=True)
        except Exception:
            _ddddocr_beta_instance = False
    return _ddddocr_beta_instance if _ddddocr_beta_instance is not False else None


def _build_ddddocr_variants(source_bytes: bytes) -> list[bytes]:
    variants: list[bytes] = [source_bytes]
    try:
        image = Image.open(io.BytesIO(source_bytes)).convert("L")
        image.load()
    except Exception:
        return variants

    def _to_png_bytes(img: Image.Image) -> bytes:
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    auto = ImageOps.autocontrast(image)
    variants.append(_to_png_bytes(auto))
    variants.append(_to_png_bytes(ImageOps.invert(image)))
    variants.append(_to_png_bytes(ImageOps.invert(auto)))

    for threshold in (80, 100, 120):
        bw = auto.point(lambda p: 255 if p > threshold else 0)
        variants.append(_to_png_bytes(bw))
        variants.append(_to_png_bytes(ImageOps.invert(bw)))
    return variants


def _recognize_with_ddddocr(image_bytes: bytes, provider: str | None = None) -> tuple[str, float] | None:
    engine = _get_ddddocr_beta()
    if engine is None:
        return None
    provider_name = str(provider or "").strip().lower()
    best_code = ""
    best_score = (-1.0, -1.0)
    best_conf = 0.0

    for sample in _build_ddddocr_variants(image_bytes):
        try:
            raw = engine.classification(sample)
        except Exception:
            continue
        code = _normalize_code(raw, provider=provider)
        if not code:
            continue

        if provider_name in {"subhd", "subhdtw"}:
            score = (1.0 if len(code) == 4 else 0.0, float(len(code)))
            conf = 0.90 if len(code) == 4 else 0.65
        else:
            score = (float(len(code)), 0.0)
            conf = 0.80

        if score > best_score:
            best_score = score
            best_code = code
            best_conf = conf
            if provider_name in {"subhd", "subhdtw"} and len(code) == 4:
                break

    if not best_code:
        return None
    return best_code, best_conf


def _preprocess_variants(img: Image.Image) -> Iterable[Image.Image]:
    gray = ImageOps.grayscale(img)
    gray = ImageOps.autocontrast(gray)
    blur = gray.filter(ImageFilter.MedianFilter(size=3))

    bin_145 = blur.point(lambda p: 255 if p > 145 else 0)
    bin_165 = blur.point(lambda p: 255 if p > 165 else 0)

    yield gray
    yield blur
    yield bin_145
    yield bin_165


def _extract_confidence(image: Image.Image) -> float:
    try:
        data = pytesseract.image_to_data(
            image,
            config="--psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
            output_type=pytesseract.Output.DICT,
        )
    except Exception:
        return 0.0
    scores = []
    for raw in data.get("conf", []):
        try:
            conf = float(raw)
        except Exception:
            continue
        if conf >= 0:
            scores.append(conf)
    if not scores:
        return 0.0
    return min(1.0, max(0.0, (sum(scores) / len(scores)) / 100.0))


def _recognize(image: Image.Image, provider: str | None = None) -> tuple[str, float]:
    best_code = ""
    best_conf = 0.0
    best_score: tuple[float, float, float] = (0.0, 0.0, 0.0)
    provider_name = str(provider or "").strip().lower()
    config = "--psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

    for variant in _preprocess_variants(image):
        try:
            raw = pytesseract.image_to_string(variant, config=config)
        except Exception:
            continue
        code = _normalize_code(raw, provider=provider)
        if not code:
            continue
        conf = _extract_confidence(variant)
        if provider_name in {"subhd", "subhdtw"}:
            score = (
                1.0 if len(code) == 4 else 0.0,
                max(0.0, 1.0 - abs(len(code) - 4) / 4.0),
                conf,
            )
        else:
            score = (float(len(code)), conf, 0.0)
        if score > best_score:
            best_score = score
            best_code = code
            best_conf = conf
    return best_code, round(best_conf, 4)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "name": "subtitle-captcha-ocr", "version": "0.1.0"}


@app.post("/ocr", response_model=OCRResponse)
def ocr(payload: OCRRequest) -> OCRResponse:
    if not payload.image_base64:
        return OCRResponse(code="", confidence=0.0, engine="none")
    try:
        image_bytes = base64.b64decode(payload.image_base64, validate=True)
    except Exception:
        return OCRResponse(code="", confidence=0.0, engine="none")

    svg_decoded = _decode_svg_hint(image_bytes)
    if svg_decoded is not None:
        code, confidence = svg_decoded
        return OCRResponse(code=code, confidence=confidence, engine="svg")

    source_bytes = image_bytes
    if _is_svg(image_bytes):
        rendered = _svg_bytes_to_png(image_bytes)
        if rendered:
            source_bytes = rendered

    dddd_result = _recognize_with_ddddocr(source_bytes, provider=payload.provider)
    if dddd_result is not None:
        code, confidence = dddd_result
        return OCRResponse(code=code, confidence=confidence, engine="ddddocr")

    try:
        image = Image.open(io.BytesIO(source_bytes))
        image.load()
    except Exception:
        return OCRResponse(code="", confidence=0.0, engine="none")

    code, confidence = _recognize(image, provider=payload.provider)
    return OCRResponse(code=code, confidence=confidence, engine="tesseract")
