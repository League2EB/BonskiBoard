"""FastAPI entry point for the local-network BonskiBoard service."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import re
import secrets
from tempfile import TemporaryDirectory
from typing import Optional, Protocol

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.datastructures import UploadFile

from .feishu import FeishuSubmitter, SubmissionResult
from .images import SanitizedPhoto, sanitize_upload
from .security import (
    ApiError,
    MaxRequestBodySizeMiddleware,
    SecurityHeadersMiddleware,
    SlidingWindowRateLimiter,
    client_ip,
    error_response,
    require_same_origin_request,
)
from .settings import Settings


LOGGER = logging.getLogger("bonskiboard")
ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT_DIR / "app" / "static"
THEME_FILE = ROOT_DIR / "pokemon-theme.css"

NAME_PATTERN = re.compile(r"^[\w\s\-./()（）·]+$", re.UNICODE)
BOARD_NUMBER_PATTERN = re.compile(r"^[A-Za-z0-9._#\-/]+$")
REQUIRED_FIELDS = {
    "name",
    "board_number",
    "ski_type",
    "board_photo",
    "card_front_photo",
    "card_back_photo",
}
SKI_TYPES = {"single", "double"}


class Submitter(Protocol):
    async def submit(
        self,
        *,
        name: str,
        board_number: str,
        ski_type: str,
        photos: dict[str, SanitizedPhoto],
    ) -> SubmissionResult: ...


def create_app(
    settings: Optional[Settings] = None,
    submitter: Optional[Submitter] = None,
) -> FastAPI:
    """Create a testable app instance without storing any user information."""

    runtime_settings = settings or Settings.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime_settings.temp_root.mkdir(parents=True, exist_ok=True)
        yield

    app = FastAPI(
        title="BonskiBoard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = runtime_settings
    app.state.submitter = submitter or FeishuSubmitter(runtime_settings)
    app.state.rate_limiter = SlidingWindowRateLimiter(
        limit=runtime_settings.rate_limit_requests,
        window_seconds=runtime_settings.rate_limit_window_seconds,
    )
    app.state.submission_semaphore = asyncio.Semaphore(
        runtime_settings.max_concurrent_submissions
    )

    # The last added middleware is outermost, so headers also cover size errors.
    app.add_middleware(
        MaxRequestBodySizeMiddleware,
        max_bytes=runtime_settings.max_request_bytes,
    )
    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, error: ApiError) -> JSONResponse:
        return error_response(error)

    @app.get("/", include_in_schema=False)
    async def homepage() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/pokemon-theme.css", include_in_schema=False)
    async def pokemon_theme() -> FileResponse:
        return FileResponse(THEME_FILE, media_type="text/css")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "manifest.webmanifest",
            media_type="application/manifest+json",
        )

    @app.get("/service-worker.js", include_in_schema=False)
    async def service_worker() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "service-worker.js",
            media_type="application/javascript",
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/submissions", include_in_schema=False)
    async def create_submission(request: Request) -> JSONResponse:
        require_same_origin_request(request)
        if not await app.state.rate_limiter.allow(client_ip(request)):
            raise ApiError(
                "rate_limited",
                429,
                "送出次數太頻繁，請稍後再試。",
            )

        name, board_number, ski_type, uploads = await _parse_submission_form(request)
        request_id = secrets.token_hex(8)
        LOGGER.info("submission request_id=%s phase=accepted", request_id)

        try:
            with TemporaryDirectory(
                prefix="bonski-",
                dir=app.state.settings.temp_root,
            ) as temporary_directory:
                destination = Path(temporary_directory)
                photos = {
                    role: await sanitize_upload(
                        upload,
                        role=role,
                        destination=destination,
                        settings=app.state.settings,
                    )
                    for role, upload in uploads.items()
                }

                try:
                    async with app.state.submission_semaphore:
                        result = await asyncio.wait_for(
                            app.state.submitter.submit(
                                name=name,
                                board_number=board_number,
                                ski_type=ski_type,
                                photos=photos,
                            ),
                            timeout=app.state.settings.request_timeout_seconds,
                        )
                except TimeoutError as exc:
                    raise ApiError(
                        "submit_timeout",
                        504,
                        "送出逾時，請稍後再試。",
                    ) from exc
        except ApiError as exc:
            LOGGER.warning(
                "submission request_id=%s phase=failed code=%s",
                request_id,
                exc.code,
            )
            raise
        except Exception:
            LOGGER.exception("submission request_id=%s phase=failed code=internal_error", request_id)
            raise ApiError(
                "submit_failed",
                502,
                "送出失敗，請稍後再試。",
            )
        finally:
            for upload in uploads.values():
                await upload.close()

        LOGGER.info("submission request_id=%s phase=completed", request_id)
        return JSONResponse(
            {
                "ok": True,
                "status": result.status,
                "message": result.message,
            }
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


async def _parse_submission_form(
    request: Request,
) -> tuple[str, str, str, dict[str, UploadFile]]:
    """Accept exactly the expected multipart schema, with no arbitrary fields."""

    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        raise ApiError(
            "validation_failed",
            415,
            "送出格式不正確，請從本站重新操作。",
        )

    try:
        form = await request.form(
            max_files=3,
            max_fields=6,
        )
    except Exception as exc:
        raise ApiError(
            "validation_failed",
            422,
            "送出資料格式不正確，請重新選擇照片後再試。",
        ) from exc

    values: dict[str, list[object]] = defaultdict(list)
    for key, value in form.multi_items():
        values[key].append(value)

    if set(values) != REQUIRED_FIELDS or any(len(items) != 1 for items in values.values()):
        raise ApiError(
            "validation_failed",
            422,
            "請填寫姓名、寄存編號、雪板類型，並選擇三張照片。",
        )

    name_value = values["name"][0]
    board_number_value = values["board_number"][0]
    ski_type_value = values["ski_type"][0]
    if (
        not isinstance(name_value, str)
        or not isinstance(board_number_value, str)
        or not isinstance(ski_type_value, str)
    ):
        raise ApiError(
            "validation_failed",
            422,
            "姓名、寄存編號或雪板類型格式不正確。",
        )
    name = _validate_name(name_value)
    board_number = _validate_board_number(board_number_value)
    ski_type = _validate_ski_type(ski_type_value)

    uploads: dict[str, UploadFile] = {}
    for role in ("board_photo", "card_front_photo", "card_back_photo"):
        value = values[role][0]
        if not isinstance(value, UploadFile):
            raise ApiError(
                "validation_failed",
                422,
                "請選擇三張照片。",
            )
        uploads[role] = value

    return name, board_number, ski_type, uploads


def _validate_name(value: str) -> str:
    name = value.strip()
    if not (1 <= len(name) <= 80) or not NAME_PATTERN.fullmatch(name):
        raise ApiError(
            "validation_failed",
            422,
            "姓名格式不正確。",
        )
    return name


def _validate_board_number(value: str) -> str:
    board_number = value.strip()
    if not (1 <= len(board_number) <= 40) or not BOARD_NUMBER_PATTERN.fullmatch(
        board_number
    ):
        raise ApiError(
            "validation_failed",
            422,
            "寄存編號格式不正確。",
        )
    return board_number


def _validate_ski_type(value: str) -> str:
    if value not in SKI_TYPES:
        raise ApiError(
            "validation_failed",
            422,
            "雪板類型格式不正確。",
        )
    return value


app = create_app()
