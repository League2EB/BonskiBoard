"""Request boundary checks and small in-memory abuse controls."""

from __future__ import annotations

from collections import defaultdict, deque
import asyncio
from dataclasses import dataclass
import time
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request
from starlette.responses import JSONResponse, Response


@dataclass
class ApiError(Exception):
    """A safe, fixed-code API error that never includes user input."""

    code: str
    status_code: int
    message: str


def error_response(error: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={"ok": False, "code": error.code, "message": error.message},
    )


class SlidingWindowRateLimiter:
    """A bounded per-IP limiter suitable for a small trusted LAN service."""

    def __init__(self, *, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, client_ip: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window_seconds
        async with self._lock:
            timestamps = self._requests[client_ip]
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= self._limit:
                return False
            timestamps.append(now)
            return True


def require_same_origin_request(request: Request) -> None:
    """Require a browser fetch emitted by the page served from this host."""

    if request.headers.get("x-bonski-request") != "1":
        raise ApiError(
            "validation_failed",
            403,
            "請從 BonskiBoard 網頁送出。",
        )

    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not origin or not host:
        raise ApiError(
            "validation_failed",
            403,
            "無法驗證送出來源，請從本站重新操作。",
        )

    parsed = urlsplit(origin)
    origin_host = parsed.netloc.lower()
    if parsed.scheme not in {"http", "https"} or origin_host != host.lower():
        raise ApiError(
            "validation_failed",
            403,
            "送出來源與目前網站不符。",
        )


def client_ip(request: Request) -> str:
    """Do not trust proxy headers in the local-network first release."""

    return request.client.host if request.client else "unknown"


class MaxRequestBodySizeMiddleware:
    """Reject requests above the configured cap before multipart parsing."""

    def __init__(self, app: Callable[..., Awaitable[None]], max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > self.max_bytes:
                    await self._send_too_large(scope, receive, send)
                    return
            except ValueError:
                await self._send_too_large(scope, receive, send)
                return

        received = 0
        response_started = False

        async def limited_receive() -> dict:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestBodyTooLarge
            return message

        async def tracked_send(message: dict) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if not response_started:
                await self._send_too_large(scope, receive, send)

    @staticmethod
    async def _send_too_large(scope: dict, receive: Callable, send: Callable) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "ok": False,
                "code": "image_too_large",
                "message": "送出資料超過大小限制。",
            },
        )
        await response(scope, receive, send)


class RequestBodyTooLarge(Exception):
    """Internal signal used by MaxRequestBodySizeMiddleware."""


class SecurityHeadersMiddleware:
    """Apply headers to every API and static-page response."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        async def secure_send(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (
                            b"content-security-policy",
                            b"default-src 'self'; base-uri 'self'; "
                            b"form-action 'self'; frame-ancestors 'none'; "
                            b"img-src 'self' blob: data:; object-src 'none'; "
                            b"script-src 'self'; style-src 'self'; "
                            b"connect-src 'self'; manifest-src 'self'; worker-src 'self'",
                        ),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
                        (
                            b"permissions-policy",
                            b"camera=(), microphone=(), geolocation=()",
                        ),
                        (b"cross-origin-resource-policy", b"same-origin"),
                    ]
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, secure_send)
