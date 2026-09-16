from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

from app.security import ApiError

from .helpers import (
    jpeg_bytes,
    same_origin_headers,
    valid_submission_data,
    valid_submission_files,
)


def submit(client, *, data=None, files=None, headers=None):
    return client.post(
        "/api/submissions",
        data=data or valid_submission_data(),
        files=files or valid_submission_files(),
        headers=headers or same_origin_headers(),
    )


def test_healthz_and_security_headers(client) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_submission_sanitizes_images_and_cleans_temporary_files(
    client, fake_submitter, settings
) -> None:
    response = submit(client)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "status": "dry_run_complete",
        "message": "已完成填寫與附件上傳檢查，未建立正式申請。",
    }
    assert len(fake_submitter.calls) == 1
    assert fake_submitter.calls[0]["name"] == "測試使用者"
    assert fake_submitter.calls[0]["board_number"] == "B413"

    for image_bytes in fake_submitter.calls[0]["photos"].values():
        with Image.open(BytesIO(image_bytes)) as image:
            assert image.format == "JPEG"
            assert image.getexif() == {}

    assert list(Path(settings.temp_root).iterdir()) == []


def test_submission_failure_still_cleans_temporary_files(settings) -> None:
    class FailingSubmitter:
        async def submit(self, **_kwargs):
            raise ApiError("submit_failed", 502, "送出失敗。")

    from fastapi.testclient import TestClient
    from app.main import create_app

    with TestClient(create_app(settings=settings, submitter=FailingSubmitter())) as client:
        response = submit(client)

    assert response.status_code == 502
    assert response.json()["code"] == "submit_failed"
    assert list(Path(settings.temp_root).iterdir()) == []


def test_submission_requires_same_origin_header(client) -> None:
    response = client.post(
        "/api/submissions",
        data=valid_submission_data(),
        files=valid_submission_files(),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "validation_failed"


def test_submission_rejects_extra_or_missing_fields(client) -> None:
    data = valid_submission_data() | {"unexpected": "value"}
    response = submit(client, data=data)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"


def test_submission_rejects_non_image_upload(client) -> None:
    files = valid_submission_files()
    files["board_photo"] = ("not-an-image.txt", b"not an image", "text/plain")
    response = submit(client, files=files)

    assert response.status_code == 422
    assert response.json()["code"] == "unsupported_image"


def test_submission_rejects_invalid_board_number(client) -> None:
    data = valid_submission_data() | {"board_number": "B 413"}
    response = submit(client, data=data)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"


def test_request_body_limit_is_enforced(settings, fake_submitter) -> None:
    limited_settings = settings.__class__(
        **{
            **settings.__dict__,
            "max_request_bytes": 16,
        }
    )
    from fastapi.testclient import TestClient
    from app.main import create_app

    with TestClient(create_app(limited_settings, fake_submitter)) as client:
        response = client.post(
            "/api/submissions",
            content=b"x" * 17,
            headers={"content-length": "17"},
        )

    assert response.status_code == 413
    assert response.json()["code"] == "image_too_large"


def test_submission_rate_limit_is_enforced(settings, fake_submitter) -> None:
    limited_settings = settings.__class__(
        **{
            **settings.__dict__,
            "rate_limit_requests": 1,
        }
    )
    from fastapi.testclient import TestClient
    from app.main import create_app

    with TestClient(create_app(limited_settings, fake_submitter)) as client:
        first = submit(client)
        second = submit(client)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["code"] == "rate_limited"


def test_static_assets_are_available(client) -> None:
    html = client.get("/")
    javascript = client.get("/static/app.js")
    theme = client.get("/pokemon-theme.css")

    assert html.status_code == 200
    assert "BonskiBoard" in html.text
    storage = client.get("/static/storage.js")
    assert javascript.status_code == 200
    assert "localStorage" in storage.text
    assert "indexedDB" in storage.text
    assert theme.status_code == 200
    assert "--primary: #5acdbd" in theme.text
