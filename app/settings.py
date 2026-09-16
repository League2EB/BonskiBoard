"""Runtime configuration for BonskiBoard.

All settings have safe local-network defaults. Secrets, user data, and form
targets are intentionally not configurable through requests.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


FEISHU_FORM_URL = (
    "https://sunac.feishu.cn/share/base/form/"
    "shrcndRhW3v7obxceOEymUijK1b?chunked=false"
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


@dataclass(frozen=True)
class Settings:
    """Configuration with intentionally bounded resource limits."""

    dry_run: bool = True
    feishu_form_url: str = FEISHU_FORM_URL
    chromium_executable_path: str | None = None
    max_request_bytes: int = 18 * 1024 * 1024
    max_image_bytes: int = 5 * 1024 * 1024
    max_image_pixels: int = 16_000_000
    output_max_edge: int = 2048
    output_jpeg_quality: int = 91
    request_timeout_seconds: int = 100
    playwright_timeout_ms: int = 80_000
    max_concurrent_submissions: int = 2
    rate_limit_requests: int = 5
    rate_limit_window_seconds: int = 10 * 60
    temp_root: Path = Path("/tmp")

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            dry_run=_env_bool("DRY_RUN", True),
            chromium_executable_path=os.getenv("CHROMIUM_EXECUTABLE_PATH"),
            max_request_bytes=_env_int("MAX_REQUEST_BYTES", 18 * 1024 * 1024),
            max_image_bytes=_env_int("MAX_IMAGE_BYTES", 5 * 1024 * 1024),
            max_image_pixels=_env_int("MAX_IMAGE_PIXELS", 16_000_000),
            output_max_edge=_env_int("OUTPUT_MAX_EDGE", 2048),
            output_jpeg_quality=_env_int("OUTPUT_JPEG_QUALITY", 91),
            request_timeout_seconds=_env_int("REQUEST_TIMEOUT_SECONDS", 100),
            playwright_timeout_ms=_env_int("PLAYWRIGHT_TIMEOUT_MS", 80_000),
            max_concurrent_submissions=_env_int("MAX_CONCURRENT_SUBMISSIONS", 2),
            rate_limit_requests=_env_int("RATE_LIMIT_REQUESTS", 5),
            rate_limit_window_seconds=_env_int("RATE_LIMIT_WINDOW_SECONDS", 10 * 60),
            temp_root=Path(os.getenv("TEMP_ROOT", "/tmp")),
        )
