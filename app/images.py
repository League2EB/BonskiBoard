"""Image validation and defensive metadata stripping."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from fastapi import UploadFile
from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError

from .security import ApiError
from .settings import Settings


ImageFile.LOAD_TRUNCATED_IMAGES = False
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


@dataclass(frozen=True)
class SanitizedPhoto:
    role: str
    path: Path


async def sanitize_upload(
    upload: UploadFile,
    *,
    role: str,
    destination: Path,
    settings: Settings,
) -> SanitizedPhoto:
    """Decode, orient, resize, and re-encode a client image as a clean JPEG."""

    if upload.content_type not in ALLOWED_CONTENT_TYPES:
        raise ApiError(
            "unsupported_image",
            422,
            "照片必須是 JPEG、PNG 或 WebP 圖片。",
        )

    source = await upload.read(settings.max_image_bytes + 1)
    if not source:
        raise ApiError("unsupported_image", 422, "照片檔案不能是空的。")
    if len(source) > settings.max_image_bytes:
        raise ApiError(
            "image_too_large",
            413,
            "單張照片超過大小限制，請選擇較小的圖片。",
        )

    target = destination / f"{role}.jpg"
    try:
        _reencode(source, target, settings)
    except Image.DecompressionBombError as exc:
        raise ApiError(
            "image_too_large",
            413,
            "照片解析度過大，請選擇較小的圖片。",
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ApiError(
            "unsupported_image",
            422,
            "無法讀取這張照片，請重新選擇有效圖片。",
        ) from exc

    return SanitizedPhoto(role=role, path=target)


def _reencode(source: bytes, target: Path, settings: Settings) -> None:
    """Synchronous Pillow work kept separate for direct unit testing."""

    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = settings.max_image_pixels
    try:
        with Image.open(BytesIO(source)) as source_image:
            source_image.load()
            pixel_count = source_image.width * source_image.height
            if pixel_count > settings.max_image_pixels:
                raise Image.DecompressionBombError(
                    f"Image has {pixel_count} pixels, above the configured limit."
                )

            image = ImageOps.exif_transpose(source_image)
            if image.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", image.size, "white")
                alpha = image.getchannel("A")
                background.paste(image.convert("RGB"), mask=alpha)
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")

            if max(image.size) > settings.output_max_edge:
                image.thumbnail(
                    (settings.output_max_edge, settings.output_max_edge),
                    Image.Resampling.LANCZOS,
                )

            image.save(
                target,
                format="JPEG",
                quality=settings.output_jpeg_quality,
                optimize=True,
                progressive=True,
            )
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit
