from __future__ import annotations

from io import BytesIO

from PIL import Image


def jpeg_bytes(*, size: tuple[int, int] = (80, 120), include_exif: bool = False) -> bytes:
    image = Image.new("RGB", size, "#5acdbd")
    output = BytesIO()
    if include_exif:
        exif = Image.Exif()
        exif[271] = "Bonski Test Camera"
        exif[306] = "2026:09:16 12:00:00"
        image.save(output, format="JPEG", exif=exif)
    else:
        image.save(output, format="JPEG")
    return output.getvalue()


def valid_submission_files() -> dict[str, tuple[str, bytes, str]]:
    return {
        "board_photo": ("board.jpeg", jpeg_bytes(include_exif=True), "image/jpeg"),
        "card_front_photo": ("front.jpeg", jpeg_bytes(), "image/jpeg"),
        "card_back_photo": ("back.jpeg", jpeg_bytes(), "image/jpeg"),
    }


def valid_submission_data() -> dict[str, str]:
    return {"name": "測試使用者", "board_number": "B413"}


def same_origin_headers() -> dict[str, str]:
    return {
        "origin": "http://testserver",
        "host": "testserver",
        "x-bonski-request": "1",
    }
