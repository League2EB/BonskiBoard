from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest

from app.main import _submit_within_deadline
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
        "message": "檢查完成，未正式送出。",
    }
    assert len(fake_submitter.calls) == 1
    assert fake_submitter.calls[0]["name"] == "SYNTHETIC-USER"
    assert fake_submitter.calls[0]["board_number"] == "TEST-0001"
    assert fake_submitter.calls[0]["ski_type"] == "single"

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


def test_form_unavailable_returns_retryable_status(settings) -> None:
    class UnavailableSubmitter:
        async def submit(self, **_kwargs):
            raise ApiError(
                "form_unavailable",
                503,
                "飛書表單暫時無法載入，請稍後再試。",
            )

    from fastapi.testclient import TestClient
    from app.main import create_app

    with TestClient(
        create_app(settings=settings, submitter=UnavailableSubmitter())
    ) as client:
        response = submit(client)

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "code": "form_unavailable",
        "message": "飛書表單暫時無法載入，請稍後再試。",
    }


def test_submission_deadline_includes_waiting_for_concurrency_slot() -> None:
    class UnexpectedSubmitter:
        async def submit(self, **_kwargs):
            raise AssertionError("submitter must not run after queue timeout")

    async def exercise() -> None:
        semaphore = asyncio.Semaphore(1)
        await semaphore.acquire()
        with pytest.raises(asyncio.TimeoutError):
            await _submit_within_deadline(
                semaphore=semaphore,
                submitter=UnexpectedSubmitter(),
                name="Test",
                board_number="123",
                ski_type="single",
                photos={},
                timeout_seconds=0.001,
            )
        semaphore.release()

    asyncio.get_event_loop().run_until_complete(exercise())


def test_submission_requires_same_origin_header(client) -> None:
    response = client.post(
        "/api/submissions",
        data=valid_submission_data(),
        files=valid_submission_files(),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "validation_failed"


def test_submission_rejects_extra_fields(client) -> None:
    data = valid_submission_data() | {"unexpected": "value"}
    response = submit(client, data=data)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"


def test_submission_rejects_missing_ski_type_before_downstream_submission(
    client, fake_submitter
) -> None:
    data = valid_submission_data()
    data.pop("ski_type")

    response = submit(client, data=data)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert fake_submitter.calls == []


def test_submission_rejects_invalid_ski_type_before_downstream_submission(
    client, fake_submitter
) -> None:
    response = submit(
        client,
        data=valid_submission_data() | {"ski_type": "single-board"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert fake_submitter.calls == []


def test_submission_rejects_duplicate_ski_type_before_downstream_submission(
    client, fake_submitter
) -> None:
    data = valid_submission_data()
    files = valid_submission_files()
    multipart_parts = [
        ("name", (None, data["name"])),
        ("board_number", (None, data["board_number"])),
        ("ski_type", (None, "single")),
        ("ski_type", (None, "double")),
        ("board_photo", files["board_photo"]),
        ("card_front_photo", files["card_front_photo"]),
        ("card_back_photo", files["card_back_photo"]),
    ]
    response = client.post(
        "/api/submissions",
        files=multipart_parts,
        headers=same_origin_headers(),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert fake_submitter.calls == []


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


def test_submission_allows_repeated_requests(client, fake_submitter) -> None:
    responses = [submit(client) for _ in range(6)]

    assert [response.status_code for response in responses] == [200] * 6
    assert len(fake_submitter.calls) == 6


def test_static_assets_are_available(client) -> None:
    html = client.get("/")
    javascript = client.get("/static/app.js")
    theme = client.get("/pokemon-theme.css")
    icon = client.get("/static/icon.png")

    assert html.status_code == 200
    assert "BonskiBoard" in html.text
    assert 'property="og:type" content="website"' in html.text
    assert 'property="og:site_name" content="BonskiBoard"' in html.text
    assert 'property="og:title" content="BonskiBoard 領板申請"' in html.text
    assert (
        'property="og:description" content="BonskiBoard 廣州融創寄存區領板助手"'
        in html.text
    )
    assert (
        'property="og:image" content="https://board.dadaderme.me/static/bonskiboard-share-card-df68329f.png"'
        in html.text
    )
    assert 'property="og:image:type" content="image/png"' in html.text
    assert 'property="og:image:width" content="1200"' in html.text
    assert 'property="og:image:height" content="630"' in html.text
    assert (
        'property="og:image:alt" content="BonskiBoard 廣州融創寄存區領板助手"'
        in html.text
    )
    assert 'name="twitter:card" content="summary_large_image"' in html.text
    assert 'name="twitter:title" content="BonskiBoard 領板申請"' in html.text
    assert (
        'name="twitter:description" content="BonskiBoard 廣州融創寄存區領板助手"'
        in html.text
    )
    assert (
        'name="twitter:image" content="https://board.dadaderme.me/static/bonskiboard-share-card-df68329f.png"'
        in html.text
    )
    assert (
        'name="twitter:image:alt" content="BonskiBoard 廣州融創寄存區領板助手"'
        in html.text
    )
    assert 'rel="icon" href="/static/icon.png" type="image/png"' in html.text
    assert 'rel="apple-touch-icon" href="/static/icon.png"' in html.text
    assert '<img src="/static/icon.png" alt="" />' in html.text
    storage = client.get("/static/storage.js")
    assert javascript.status_code == 200
    assert storage.status_code == 200
    assert icon.status_code == 200
    assert icon.headers["content-type"] == "image/png"
    assert icon.content == (Path(__file__).resolve().parents[1] / "icon.png").read_bytes()
    share_card = client.get("/static/bonskiboard-share-card-df68329f.png")
    assert share_card.status_code == 200
    assert share_card.headers["content-type"] == "image/png"
    assert share_card.content == (
        Path(__file__).resolve().parents[1] / "bonskiboard-share-card-final.png"
    ).read_bytes()
    assert "localStorage" in storage.text
    assert "indexedDB" in storage.text
    assert "LAST_CONFIRMED_SUBMISSION_TIME_KEY" in storage.text
    assert "loadLastConfirmedSubmissionTime" in storage.text
    assert "saveLastConfirmedSubmissionTime" in storage.text
    assert "removeLastConfirmedSubmissionTime" in storage.text
    assert 'name="ski_type"' in html.text
    assert 'value="single"' in html.text
    assert 'value="double"' in html.text
    assert "schemaVersion: 2" in storage.text
    assert "profile.schemaVersion === 1" in storage.text
    assert 'profile.skiType === "single" || profile.skiType === "double"' in storage.text
    assert "needsSkiTypeSelection" in storage.text
    assert 'data-quick-submit' in html.text
    assert 'id="quick-submit-button"' in html.text
    assert "quickSubmitButton.addEventListener" in javascript.text
    assert "renderQuickSubmit" in javascript.text
    assert 'id="success-dialog"' in html.text
    assert 'data-quick-last-submission' in html.text
    assert 'data-form-last-submission' in html.text
    assert "showSubmissionSuccess" in javascript.text
    assert 'payload.status === "submitted"' in javascript.text
    assert 'busy ? "處理中" : "送出領板申請"' in javascript.text
    assert "這幹嘛的？" in html.text
    assert "目前只支援廣州融創滑雪場寄存區" in html.text
    assert "非官方工具" in html.text
    assert 'href="#about"' in html.text
    assert 'href="#details"' in html.text
    assert 'href="#photos"' in html.text
    assert 'href="#submit"' in html.text
    assert "卡號需清楚可見" in html.text
    assert "個人照片與有效期需清楚可見" in html.text
    assert "點選「再次填寫」" in html.text
    assert "重新輸入資料、重新選照片，很麻煩。" in html.text
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert '"description": "廣州融創寄存區領板助手"' in manifest.text
    assert '"src": "/static/icon.png"' in manifest.text
    assert '"sizes": "1254x1254"' in manifest.text
    service_worker = client.get("/service-worker.js")
    assert "bonski-board-v9" in service_worker.text
    assert '"/static/icon.png"' in service_worker.text
    assert theme.status_code == 200
    assert "--primary: #5acdbd" in theme.text
