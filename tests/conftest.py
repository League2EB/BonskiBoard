from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.feishu import SubmissionResult
from app.main import create_app
from app.settings import Settings


@dataclass
class FakeSubmitter:
    result: SubmissionResult = field(
        default_factory=lambda: SubmissionResult(
            status="dry_run_complete",
            message="已完成填寫與附件上傳檢查，未建立正式申請。",
        )
    )
    calls: list[dict] = field(default_factory=list)

    async def submit(self, *, name: str, board_number: str, photos: dict) -> SubmissionResult:
        self.calls.append(
            {
                "name": name,
                "board_number": board_number,
                "photos": {
                    role: photo.path.read_bytes() for role, photo in photos.items()
                },
            }
        )
        return self.result


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        temp_root=tmp_path,
        max_image_bytes=512 * 1024,
        max_request_bytes=2 * 1024 * 1024,
        rate_limit_requests=5,
        rate_limit_window_seconds=60,
        request_timeout_seconds=2,
    )


@pytest.fixture
def fake_submitter() -> FakeSubmitter:
    return FakeSubmitter()


@pytest.fixture
def app(settings: Settings, fake_submitter: FakeSubmitter):
    return create_app(settings=settings, submitter=fake_submitter)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client
