from __future__ import annotations

import asyncio
from io import BytesIO

import pytest
from PIL import Image
from starlette.datastructures import Headers, UploadFile

from app.images import sanitize_upload
from app.security import ApiError

from .helpers import jpeg_bytes


def upload_from_bytes(content: bytes, content_type: str = "image/jpeg") -> UploadFile:
    return UploadFile(
        file=BytesIO(content),
        filename="image.jpg",
        headers=Headers({"content-type": content_type}),
    )


def test_sanitize_upload_removes_exif_and_resizes(tmp_path, settings) -> None:
    source = jpeg_bytes(size=(3000, 1800), include_exif=True)
    photo = asyncio.run(
        sanitize_upload(
            upload_from_bytes(source),
            role="board_photo",
            destination=tmp_path,
            settings=settings,
        )
    )

    with Image.open(photo.path) as image:
        assert image.format == "JPEG"
        assert max(image.size) == settings.output_max_edge
        assert image.getexif() == {}


def test_sanitize_upload_rejects_wrong_content_type(tmp_path, settings) -> None:
    with pytest.raises(ApiError, match="JPEG"):
        asyncio.run(
            sanitize_upload(
                upload_from_bytes(b"plain text", "text/plain"),
                role="board_photo",
                destination=tmp_path,
                settings=settings,
            )
        )


def test_sanitize_upload_rejects_bad_image_data(tmp_path, settings) -> None:
    with pytest.raises(ApiError) as raised:
        asyncio.run(
            sanitize_upload(
                upload_from_bytes(b"not a jpeg"),
                role="board_photo",
                destination=tmp_path,
                settings=settings,
            )
        )

    assert raised.value.code == "unsupported_image"
