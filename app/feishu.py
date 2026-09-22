"""Conservative anonymous browser automation for the fixed Feishu form."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
import logging
from pathlib import Path
import re
from time import monotonic
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .images import SanitizedPhoto
from .security import ApiError
from .settings import Settings
from .vpn import verify_vpn_egress


FIELD_IDS = {
    "name": "fldZJlD0Ox",
    "board_number": "fldRpJzAGC",
    "ski_type": "fldFD4KWOC",
    "board_photo": "fldBBvxZS1",
    "card_photos": "fldP0AVcVb",
}

SKI_TYPE_CHOICES = {
    "single": ("单板", "optHOXFuMJ"),
    "double": ("双板", "optFPiQW0g"),
}
TEXT_CONTROL_SELECTOR = '[contenteditable="true"][data-placeholder]'
SKI_TYPE_CONTROL_SELECTOR = '[data-form-control-interactive-root="true"]'
CURRENT_FORM_CONTAINER_SELECTORS = tuple(
    f"#field-item-{FIELD_IDS[kind]}"
    for kind in (
        "name",
        "board_number",
        "ski_type",
        "board_photo",
        "card_photos",
    )
)
FORM_LOAD_ATTEMPTS = 2
FORM_NAVIGATION_TIMEOUT_MS = 15_000
FORM_READY_TIMEOUTS_MS = (30_000, 10_000)
FORM_VIEWPORTS = (
    {"width": 1280, "height": 900},
    {"width": 325, "height": 793},
)
CONTROL_STABILITY_SAMPLES = 2
CONTROL_STABILITY_TIMEOUT_SECONDS = 3.0
CONTROL_STABILITY_POLL_SECONDS = 0.1
UPLOAD_CONFIRM_TIMEOUT_SECONDS = 15.0
UPLOAD_CONFIRM_POLL_SECONDS = 0.1
PLAYWRIGHT_CHROMIUM_MAJOR_VERSION = 148

TEXT_TERMS = {
    "name": ("姓名", "name"),
    "board_number": (
        "寄存編號",
        "寄存编号",
        "寄存號",
        "寄存号",
        "storage number",
        "storage",
        "雪板編號",
        "雪板编号",
        "雪板號",
        "雪板号",
        "板號",
        "board",
    ),
}

PHOTO_TERMS = {
    "board_photo": ("雪板照片", "雪板图片", "雪板圖", "雪板"),
    "card_photos": (
        "雪卡正反面",
        "雪卡正反",
        "雪卡照片",
        "雪卡圖片",
        "雪卡",
        "會員卡",
        "会员卡",
    ),
}

LOGGER = logging.getLogger("bonskiboard.feishu")
try:
    PLAYWRIGHT_VERSION = version("playwright")
except PackageNotFoundError:
    PLAYWRIGHT_VERSION = "unknown"


@dataclass(frozen=True)
class SubmissionResult:
    status: str
    message: str


@dataclass(frozen=True)
class _PreflightedControls:
    name_input: Locator
    board_number_input: Locator
    ski_type_control: Locator
    board_photo_input: Locator
    card_photo_input: Locator
    submit_button: Locator
    attempt: int
    viewport_width: int
    viewport_height: int


class _FormLoadIncomplete(Exception):
    def __init__(
        self,
        reason: str,
        fields_found: int,
        *,
        ready_state: str = "unknown",
        body_text_length: int = 0,
        html_length: int = 0,
        resource_count: int = 0,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fields_found = fields_found
        self.ready_state = ready_state
        self.body_text_length = body_text_length
        self.html_length = html_length
        self.resource_count = resource_count


@dataclass(frozen=True)
class _PreflightDiagnostics:
    attempt: int
    viewport_width: int
    viewport_height: int


_PREFLIGHT_DIAGNOSTICS: ContextVar[Optional[_PreflightDiagnostics]] = ContextVar(
    "feishu_preflight_diagnostics",
    default=None,
)
_BROWSER_VERSION: ContextVar[str] = ContextVar(
    "feishu_browser_version",
    default="unknown",
)


class FeishuSubmitter:
    """Automate only the fixed anonymous form and fail closed on ambiguity."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def submit(
        self,
        *,
        name: str,
        board_number: str,
        ski_type: str,
        photos: dict[str, SanitizedPhoto],
        request_id: str | None = None,
    ) -> SubmissionResult:
        try:
            async with async_playwright() as playwright:
                browser: Optional[Browser] = None
                page: Optional[Page] = None
                browser_version_token: Token | None = None
                try:
                    launch_options: dict[str, Any] = {
                        "headless": True,
                        "args": ["--disable-dev-shm-usage"],
                    }
                    if self.settings.chromium_executable_path:
                        launch_options["executable_path"] = (
                            self.settings.chromium_executable_path
                        )
                        launch_options["args"].append("--no-sandbox")
                    try:
                        browser = await playwright.chromium.launch(**launch_options)
                    except PlaywrightError as exc:
                        diagnostic = (
                            "browser_version_mismatch"
                            if self._is_browser_version_mismatch(exc)
                            else "browser_launch_failed"
                        )
                        LOGGER.warning(
                            "feishu_browser diagnostic=%s playwright_version=%s "
                            "chromium_version=unknown source=%s",
                            diagnostic,
                            PLAYWRIGHT_VERSION,
                            self._browser_source(),
                        )
                        raise self._form_unavailable(
                            reason=diagnostic,
                            attempts=0,
                            fields_found=0,
                        ) from exc
                    chromium_version = getattr(browser, "version", "unknown")
                    if not self._is_expected_chromium_version(chromium_version):
                        LOGGER.warning(
                            "feishu_browser diagnostic=browser_version_mismatch "
                            "playwright_version=%s chromium_version=%s source=%s "
                            "expected_chromium_major=%s",
                            PLAYWRIGHT_VERSION,
                            chromium_version,
                            self._browser_source(),
                            PLAYWRIGHT_CHROMIUM_MAJOR_VERSION,
                        )
                        raise self._form_unavailable(
                            reason="browser_version_mismatch",
                            attempts=0,
                            fields_found=0,
                        )
                    LOGGER.info(
                        "feishu_browser playwright_version=%s chromium_version=%s "
                        "source=%s",
                        PLAYWRIGHT_VERSION,
                        chromium_version,
                        self._browser_source(),
                    )
                    browser_version_token = _BROWSER_VERSION.set(chromium_version)
                    context = await browser.new_context(
                        accept_downloads=False,
                        viewport=FORM_VIEWPORTS[0],
                        locale="zh-TW",
                    )
                    completed_form = await self._complete_form(
                        context=context,
                        name=name,
                        board_number=board_number,
                        ski_type=ski_type,
                        photos=photos,
                    )
                    if isinstance(completed_form, tuple):
                        page, submit_button = completed_form
                    else:
                        # Retain the narrow compatibility seam used by existing
                        # in-process fakes; production _complete_form always
                        # returns the freshly preflighted page and control.
                        page = getattr(context, "page", None)
                        submit_button = completed_form
                        if page is None:
                            raise RuntimeError("form completion returned no page")
                    if self.settings.dry_run:
                        return SubmissionResult(
                            status="dry_run_complete",
                            message="檢查完成，未正式送出。",
                        )
                    egress_ip = await verify_vpn_egress(self.settings)
                    LOGGER.info(
                        "submission request_id=%s phase=vpn_verified "
                        "vpn_status=running egress_ip=%s",
                        request_id or "unknown",
                        egress_ip,
                    )
                    LOGGER.info(
                        "submission request_id=%s phase=feishu_submit_started",
                        request_id or "unknown",
                    )
                    await self._submit(page, submit_button)
                    return SubmissionResult(status="submitted", message="申請已送出。")
                finally:
                    if browser_version_token is not None:
                        _BROWSER_VERSION.reset(browser_version_token)
                    if page is not None:
                        await self._close_page(page)
                    if browser:
                        await browser.close()
        except ApiError:
            raise
        except PlaywrightTimeoutError as exc:
            raise ApiError(
                "submit_timeout",
                504,
                "飛書表單逾時，請稍後再試。",
            ) from exc
        except Exception as exc:
            raise ApiError(
                "submit_failed",
                502,
                "飛書暫時無法送出，請稍後再試。",
            ) from exc

    async def _complete_form(
        self,
        *,
        page: Page | None = None,
        context: Any | None = None,
        name: str,
        board_number: str,
        ski_type: str,
        photos: dict[str, SanitizedPhoto],
    ) -> Locator | tuple[Page, Locator]:
        """Preflight on a fresh page before a single data-entry pass."""

        loaded = await self._load_and_preflight_form(page, context=context)
        if isinstance(loaded, tuple):
            page, controls = loaded
        else:
            if page is None:
                # Existing in-process fakes can replace the loader but not
                # provide its production tuple. Real contexts never use this.
                page = getattr(context, "page", None)
            if page is None:
                raise RuntimeError("preflight completed without a page")
            controls = loaded
        token = _PREFLIGHT_DIAGNOSTICS.set(
            _PreflightDiagnostics(
                attempt=controls.attempt,
                viewport_width=controls.viewport_width,
                viewport_height=controls.viewport_height,
            )
        )
        try:
            await controls.name_input.fill(name)
            await controls.board_number_input.fill(board_number)
            await self._select_ski_type(
                page,
                ski_type,
                control=controls.ski_type_control,
            )
            await self._upload_and_confirm(
                page,
                control=controls.board_photo_input,
                kind="board_photo",
                files=str(photos["board_photo"].path),
                filenames=(photos["board_photo"].path.name,),
            )
            await self._upload_and_confirm(
                page,
                control=controls.card_photo_input,
                kind="card_photos",
                files=[
                    str(photos["card_front_photo"].path),
                    str(photos["card_back_photo"].path),
                ],
                filenames=(
                    photos["card_front_photo"].path.name,
                    photos["card_back_photo"].path.name,
                ),
            )
            if context is None:
                return controls.submit_button
            return page, controls.submit_button
        except Exception:
            # The page has received user data by this point; never reuse it.
            await self._close_page(page)
            raise
        finally:
            _PREFLIGHT_DIAGNOSTICS.reset(token)

    async def _load_and_preflight_form(
        self,
        page: Page | None = None,
        *,
        context: Any | None = None,
    ) -> _PreflightedControls | tuple[Page, _PreflightedControls]:
        """Load a complete form before any user data reaches Feishu.

        A retry is safe only here. Once this method returns, filling, uploading,
        and submitting each happen at most once.
        """

        last_reason = "form_ready_timeout"
        last_fields_found = 0
        last_ready_state = "unknown"
        last_body_text_length = 0
        last_html_length = 0
        last_resource_count = 0
        for attempt in range(1, FORM_LOAD_ATTEMPTS + 1):
            viewport = FORM_VIEWPORTS[attempt - 1]
            owns_page = context is not None
            attempt_page = await context.new_page() if owns_page else page
            if attempt_page is None:
                raise RuntimeError("form preflight requires a page or browser context")
            self._set_page_default_timeout(attempt_page)
            started = monotonic()
            LOGGER.info(
                "feishu_form_load phase=started attempt=%s viewport=%sx%s",
                attempt,
                viewport["width"],
                viewport["height"],
            )
            try:
                controls = await self._load_form_attempt(
                    attempt_page,
                    attempt=attempt,
                    viewport=viewport,
                )
            except _FormLoadIncomplete as exc:
                last_reason = exc.reason
                last_fields_found = exc.fields_found
                last_ready_state = exc.ready_state
                last_body_text_length = exc.body_text_length
                last_html_length = exc.html_length
                last_resource_count = exc.resource_count
                diagnostic = "field_timeout"
            except PlaywrightTimeoutError:
                last_reason = "navigation_timeout"
                last_fields_found = 0
                diagnostic = "navigation_timeout"
            except PlaywrightError:
                last_reason = "navigation_error"
                last_fields_found = 0
                diagnostic = "navigation_error"
            else:
                LOGGER.info(
                    "feishu_form_load phase=ready attempt=%s fields_found=%s "
                    "viewport=%sx%s elapsed_ms=%s",
                    attempt,
                    len(CURRENT_FORM_CONTAINER_SELECTORS),
                    viewport["width"],
                    viewport["height"],
                    int((monotonic() - started) * 1_000),
                )
                if context is not None:
                    return attempt_page, controls
                return controls

            phase = "retry" if attempt < FORM_LOAD_ATTEMPTS else "failed"
            LOGGER.warning(
                "feishu_form_load phase=%s diagnostic=%s attempt=%s reason=%s "
                "fields_found=%s viewport=%sx%s elapsed_ms=%s ready_state=%s "
                "body_text_length=%s html_length=%s resource_count=%s",
                phase,
                diagnostic,
                attempt,
                last_reason,
                last_fields_found,
                viewport["width"],
                viewport["height"],
                int((monotonic() - started) * 1_000),
                last_ready_state,
                last_body_text_length,
                last_html_length,
                last_resource_count,
            )
            if owns_page:
                await self._close_page(attempt_page)

        raise self._form_unavailable(
            reason=last_reason,
            attempts=FORM_LOAD_ATTEMPTS,
            fields_found=last_fields_found,
        )

    async def _load_form_attempt(
        self,
        page: Page,
        *,
        attempt: int,
        viewport: dict[str, int],
    ) -> _PreflightedControls:
        await page.set_viewport_size(viewport)
        await page.goto(
            self.settings.feishu_form_url,
            wait_until="commit",
            timeout=self._navigation_timeout_ms(),
        )
        try:
            await self._wait_for_current_form_ready(page, attempt=attempt)
            return await self._preflight_form(
                page,
                attempt=attempt,
                viewport=viewport,
            )
        except PlaywrightTimeoutError as exc:
            fields_found = await self._current_form_field_count(page)
            state = await self._current_form_load_state(page)
            raise _FormLoadIncomplete(
                "field_timeout",
                fields_found,
                **state,
            ) from exc

    @staticmethod
    def _is_browser_version_mismatch(error: PlaywrightError) -> bool:
        """Recognize only Playwright's explicit compatibility diagnostics."""

        message = str(error).casefold()
        return any(
            marker in message
            for marker in (
                "browser version mismatch",
                "incompatible browser version",
                "browser revision mismatch",
            )
        )

    def _is_expected_chromium_version(self, chromium_version: object) -> bool:
        """Require the known bundled Chromium revision when it is observable.

        Browser test doubles commonly omit ``version``. A real Playwright
        browser always reports it, so treating an unknown fake version as
        compatible preserves the narrow test seam without weakening runtime
        validation.
        """

        if not isinstance(chromium_version, str) or chromium_version == "unknown":
            return True
        match = re.match(r"^(\d+)\.", chromium_version)
        return bool(
            match
            and int(match.group(1)) == PLAYWRIGHT_CHROMIUM_MAJOR_VERSION
        )

    def _browser_source(self) -> str:
        return (
            "custom_executable"
            if self.settings.chromium_executable_path
            else "playwright_bundle"
        )

    def _navigation_timeout_ms(self) -> int:
        # Navigation and form readiness are sequential, distinct budgets. Do
        # not wrap either with another equal timeout: Playwright owns and
        # completes the navigation future before a retry closes the page.
        return min(
            self.settings.playwright_timeout_ms,
            FORM_NAVIGATION_TIMEOUT_MS,
        )

    def _form_ready_timeout_ms(self, attempt: int) -> int:
        return min(
            self.settings.playwright_timeout_ms,
            FORM_READY_TIMEOUTS_MS[attempt - 1],
        )

    def _set_page_default_timeout(self, page: Page) -> None:
        # Page always supplies this in Playwright. The getattr keeps lightweight
        # in-process test doubles compatible without affecting production.
        set_default_timeout = getattr(page, "set_default_timeout", None)
        if set_default_timeout is not None:
            set_default_timeout(self.settings.playwright_timeout_ms)

    @staticmethod
    async def _close_page(page: Page) -> None:
        close = getattr(page, "close", None)
        if close is None:
            return
        try:
            result = close()
            if hasattr(result, "__await__"):
                await result
        except PlaywrightError:
            LOGGER.warning("feishu_form_load phase=page_close_failed")

    async def _preflight_form(
        self,
        page: Page,
        *,
        attempt: int,
        viewport: dict[str, int],
    ) -> _PreflightedControls:
        """Resolve every fixed control before filling, uploading, or submitting."""

        token = _PREFLIGHT_DIAGNOSTICS.set(
            _PreflightDiagnostics(
                attempt=attempt,
                viewport_width=viewport["width"],
                viewport_height=viewport["height"],
            )
        )
        try:
            return _PreflightedControls(
                name_input=await self._resolve_control(
                    page,
                    kind="name",
                    selector=TEXT_CONTROL_SELECTOR,
                    container_fallback_selector='[contenteditable="true"]',
                    legacy_selector=(
                        '[contenteditable="true"], '
                        "input:not([type='hidden']), textarea"
                    ),
                    terms=TEXT_TERMS["name"],
                ),
                board_number_input=await self._resolve_control(
                    page,
                    kind="board_number",
                    selector=TEXT_CONTROL_SELECTOR,
                    container_fallback_selector='[contenteditable="true"]',
                    legacy_selector=(
                        '[contenteditable="true"], '
                        "input:not([type='hidden']), textarea"
                    ),
                    terms=TEXT_TERMS["board_number"],
                ),
                ski_type_control=await self._resolve_ski_type_control(page),
                board_photo_input=await self._resolve_control(
                    page,
                    kind="board_photo",
                    selector="input[type='file']",
                    container_fallback_selector=None,
                    legacy_selector="input[type='file']",
                    terms=PHOTO_TERMS["board_photo"],
                ),
                card_photo_input=await self._resolve_control(
                    page,
                    kind="card_photos",
                    selector="input[type='file']",
                    container_fallback_selector=None,
                    legacy_selector="input[type='file']",
                    terms=PHOTO_TERMS["card_photos"],
                ),
                submit_button=await self._resolve_submit_button(page),
                attempt=attempt,
                viewport_width=viewport["width"],
                viewport_height=viewport["height"],
            )
        finally:
            _PREFLIGHT_DIAGNOSTICS.reset(token)

    async def _wait_for_current_form_ready(
        self,
        page: Page,
        *,
        attempt: int,
    ) -> None:
        """Wait for every fixed field, separating an empty load from a mismatch."""

        try:
            await asyncio.gather(
                *(
                    page.locator(selector).wait_for(
                        state="attached",
                        timeout=self._form_ready_timeout_ms(attempt),
                    )
                    for selector in CURRENT_FORM_CONTAINER_SELECTORS
                )
            )
        except PlaywrightTimeoutError as exc:
            fields_found = await self._current_form_field_count(page)
            state = await self._current_form_load_state(page)
            raise _FormLoadIncomplete(
                "form_ready_timeout",
                fields_found,
                **state,
            ) from exc

        fields_found = await self._current_form_field_count(page)
        if fields_found != len(CURRENT_FORM_CONTAINER_SELECTORS):
            raise self._layout_changed("form_container_guard")

    async def _current_form_field_count(self, page: Page) -> int:
        try:
            counts = await asyncio.gather(
                *(
                    page.locator(selector).count()
                    for selector in CURRENT_FORM_CONTAINER_SELECTORS
                )
            )
        except PlaywrightError:
            return 0
        return sum(counts)

    @staticmethod
    async def _current_form_load_state(page: Page) -> dict[str, Any]:
        """Capture non-PII state to distinguish slow bootstrap from errors."""

        evaluate = getattr(page, "evaluate", None)
        if evaluate is None:
            return {}
        try:
            state = await evaluate(
                """() => ({
                    ready_state: document.readyState || 'unknown',
                    body_text_length: (document.body?.innerText || '').length,
                    html_length: document.documentElement?.outerHTML.length || 0,
                    resource_count:
                      performance.getEntriesByType('resource').length,
                })"""
            )
        except PlaywrightError:
            return {}
        if not isinstance(state, dict):
            return {}
        return {
            "ready_state": str(state.get("ready_state", "unknown")),
            "body_text_length": int(state.get("body_text_length", 0)),
            "html_length": int(state.get("html_length", 0)),
            "resource_count": int(state.get("resource_count", 0)),
        }

    async def _resolve_control(
        self,
        page: Page,
        *,
        kind: str,
        selector: str,
        container_fallback_selector: str | None,
        legacy_selector: str,
        terms: tuple[str, ...],
    ) -> Locator:
        """Resolve one control in a stable field container or safe legacy context.

        When a verified field container exists, any missing or ambiguous control
        is a hard stop. Older forms without that container retain the previous
        labelled input/textarea lookup as a narrowly scoped fallback.
        """

        field = page.locator(f"#field-item-{FIELD_IDS[kind]}")
        field_count = await field.count()
        if field_count:
            if field_count != 1:
                raise self._layout_changed("control_container_guard")
            controls = field.locator(selector)
            if (
                container_fallback_selector is not None
                and await controls.count() == 0
            ):
                controls = field.locator(container_fallback_selector)
            return await self._require_single_control(
                controls,
                kind=kind,
                require_visible=kind not in PHOTO_TERMS,
            )

        controls = page.locator(legacy_selector)
        count = await controls.count()
        candidates: list[tuple[Locator, str]] = []
        for index in range(count):
            control = controls.nth(index)
            if kind not in PHOTO_TERMS and not await control.is_visible():
                continue
            context = await control.evaluate(
                """(element) => {
                    const labels = element.labels
                      ? Array.from(element.labels).map((label) => label.innerText)
                      : [];
                    const own = [
                      element.getAttribute('aria-label'),
                      element.getAttribute('placeholder'),
                      element.getAttribute('name'),
                    ].filter(Boolean);
                    let parent = element.parentElement;
                    let nearby = '';
                    for (let depth = 0; parent && depth < 4; depth += 1) {
                      const text = (parent.innerText || '').trim();
                      if (text && text.length <= 500) {
                        nearby = text;
                        break;
                      }
                      parent = parent.parentElement;
                    }
                    return [...labels, ...own, nearby].join(' ').toLowerCase();
                }"""
            )
            candidates.append((control, context))

        matching = [
            candidate
            for candidate in candidates
            if any(term.lower() in candidate[1] for term in terms)
        ]
        if len(matching) == 1:
            return matching[0][0]
        raise self._layout_changed(
            "control_legacy_guard",
            kind=kind,
            candidate_count=len(candidates),
            visible_count=len(candidates),
        )

    async def _require_single_control(
        self,
        controls: Locator,
        *,
        kind: str,
        require_visible: bool,
    ) -> Locator:
        deadline = (
            asyncio.get_running_loop().time() + CONTROL_STABILITY_TIMEOUT_SECONDS
        )
        stable_samples = 0
        candidate_count = 0
        visible_count = 0
        while True:
            candidate_count = await controls.count()
            matches = []
            for index in range(candidate_count):
                control = controls.nth(index)
                if not require_visible or await control.is_visible():
                    matches.append(control)
            visible_count = len(matches)

            if visible_count == 1:
                stable_samples += 1
                if stable_samples >= CONTROL_STABILITY_SAMPLES:
                    return matches[0]
            else:
                stable_samples = 0

            if asyncio.get_running_loop().time() >= deadline:
                raise self._layout_changed(
                    "control_guard",
                    kind=kind,
                    candidate_count=candidate_count,
                    visible_count=visible_count,
                )
            await asyncio.sleep(CONTROL_STABILITY_POLL_SECONDS)

    async def _select_ski_type(
        self,
        page: Page,
        ski_type: str,
        *,
        control: Locator | None = None,
    ) -> None:
        if ski_type not in SKI_TYPE_CHOICES:
            raise self._layout_changed("ski_type_value_guard")

        control = control or await self._resolve_ski_type_control(page)
        await control.click()
        await self._click_expected_ski_type_option(page, ski_type)

    async def _resolve_ski_type_control(self, page: Page) -> Locator:
        field = page.locator(f"#field-item-{FIELD_IDS['ski_type']}")
        if await field.count() != 1:
            raise self._layout_changed("ski_type_control_guard")
        return await self._require_single_control(
            field.locator(SKI_TYPE_CONTROL_SELECTOR),
            kind="ski_type",
            require_visible=True,
        )

    async def _click_expected_ski_type_option(self, page: Page, ski_type: str) -> None:
        visible_text = SKI_TYPE_CHOICES[ski_type][0]
        options = page.get_by_text(visible_text, exact=True)
        matches = [
            options.nth(index)
            for index in range(await options.count())
            if await options.nth(index).is_visible()
        ]
        if len(matches) != 1:
            raise self._layout_changed("ski_type_option_guard")
        await matches[0].click()

    async def _upload_and_confirm(
        self,
        page: Page,
        *,
        control: Locator,
        kind: str,
        files: str | list[str],
        filenames: tuple[str, ...],
    ) -> None:
        try:
            await control.set_input_files(files)
        except PlaywrightError as exc:
            raise self._upload_failed(
                "set_input_files_guard",
                kind=kind,
            ) from exc
        except Exception as exc:
            raise self._upload_failed(
                "set_input_files_guard",
                kind=kind,
            ) from exc
        await self._wait_for_uploaded_files(
            page,
            kind=kind,
            filenames=filenames,
        )

    async def _wait_for_uploaded_files(
        self,
        page: Page,
        *,
        kind: str,
        filenames: tuple[str, ...],
    ) -> None:
        field = page.locator(f"#field-item-{FIELD_IDS[kind]}")
        deadline = (
            asyncio.get_running_loop().time() + UPLOAD_CONFIRM_TIMEOUT_SECONDS
        )
        field_count = 0
        candidate_count = 0
        visible_count = 0

        try:
            while True:
                field_count = await field.count()
                candidate_count = 0
                visible_count = 0
                if field_count == 1:
                    labels = [
                        field.get_by_text(filename, exact=True)
                        for filename in filenames
                    ]
                    label_counts = await asyncio.gather(
                        *(label.count() for label in labels)
                    )
                    candidate_count = sum(label_counts)
                    visible_labels = [
                        label.nth(index)
                        for label, count in zip(labels, label_counts)
                        for index in range(count)
                        if await label.nth(index).is_visible()
                    ]
                    visible_count = len(visible_labels)
                    if all(
                        count == 1 for count in label_counts
                    ) and visible_count == len(filenames):
                        return

                if asyncio.get_running_loop().time() >= deadline:
                    raise self._upload_failed(
                        "attachment_confirmation_guard",
                        kind=kind,
                        field_count=field_count,
                        candidate_count=candidate_count,
                        visible_count=visible_count,
                    )
                await asyncio.sleep(UPLOAD_CONFIRM_POLL_SECONDS)
        except PlaywrightError as exc:
            raise self._upload_failed(
                "attachment_confirmation_error",
                kind=kind,
                field_count=field_count,
                candidate_count=candidate_count,
                visible_count=visible_count,
            ) from exc

    @staticmethod
    def _upload_failed(
        reason: str,
        *,
        kind: str,
        field_count: int | None = None,
        candidate_count: int | None = None,
        visible_count: int | None = None,
    ) -> ApiError:
        diagnostics = _PREFLIGHT_DIAGNOSTICS.get()
        LOGGER.warning(
            "feishu_upload phase=failed reason=%s kind=%s field_count=%s "
            "candidate_count=%s visible_count=%s attempt=%s viewport=%sx%s "
            "playwright_version=%s chromium_version=%s",
            reason,
            kind,
            field_count if field_count is not None else "unknown",
            candidate_count if candidate_count is not None else "unknown",
            visible_count if visible_count is not None else "unknown",
            diagnostics.attempt if diagnostics else "unknown",
            diagnostics.viewport_width if diagnostics else "unknown",
            diagnostics.viewport_height if diagnostics else "unknown",
            PLAYWRIGHT_VERSION,
            _BROWSER_VERSION.get(),
        )
        return ApiError(
            "upload_failed",
            502,
            "照片上傳未完成，請稍後再試。",
        )

    async def _submit(self, page: Page, submit_button: Locator) -> None:
        try:
            async with page.expect_response(
                lambda response: (
                    "/space/api/bitable/share/content" in response.url
                    and response.request.method == "POST"
                )
            ) as response_info:
                await submit_button.click()
            response = await response_info.value
            if not response.ok:
                raise ApiError(
                    "submit_failed",
                    502,
                    "飛書未確認送出，請稍後再試。",
                )
            try:
                payload = await response.json()
            except Exception as exc:
                raise ApiError(
                    "submit_failed",
                    502,
                    "無法確認飛書送出結果，請稍後再試。",
                ) from exc
            if not is_successful_submit_response(payload):
                raise ApiError(
                    "submit_failed",
                    502,
                    "飛書未確認送出，請稍後再試。",
                )
        except PlaywrightTimeoutError as exc:
            raise ApiError(
                "submit_timeout",
                504,
                "等待飛書確認逾時，請稍後再試。",
            ) from exc

    async def _resolve_submit_button(self, page: Page) -> Locator:
        submit_button = page.get_by_role(
            "button",
            name=re.compile(r"^(送出|提交|submit)$", re.IGNORECASE),
        )
        return await self._require_single_control(
            submit_button,
            kind="submit_button",
            require_visible=True,
        )

    @staticmethod
    def _layout_changed(
        reason: str,
        *,
        kind: str | None = None,
        candidate_count: int | None = None,
        visible_count: int | None = None,
    ) -> ApiError:
        diagnostics = _PREFLIGHT_DIAGNOSTICS.get()
        LOGGER.warning(
            "feishu_form_preflight phase=failed reason=%s kind=%s "
            "candidate_count=%s visible_count=%s attempt=%s viewport=%sx%s "
            "playwright_version=%s chromium_version=%s",
            reason,
            kind or "unknown",
            candidate_count if candidate_count is not None else "unknown",
            visible_count if visible_count is not None else "unknown",
            diagnostics.attempt if diagnostics else "unknown",
            diagnostics.viewport_width if diagnostics else "unknown",
            diagnostics.viewport_height if diagnostics else "unknown",
            PLAYWRIGHT_VERSION,
            _BROWSER_VERSION.get(),
        )
        return ApiError(
            "form_layout_changed",
            502,
            "飛書表單已變更，為避免誤送，本次申請已停止。",
        )

    @staticmethod
    def _form_unavailable(
        *,
        reason: str,
        attempts: int,
        fields_found: int,
    ) -> ApiError:
        LOGGER.warning(
            "feishu_form_load phase=unavailable attempts=%s reason=%s fields_found=%s",
            attempts,
            reason,
            fields_found,
        )
        return ApiError(
            "form_unavailable",
            503,
            "飛書表單暫時無法載入，請稍後再試。",
        )


def is_successful_submit_response(payload: object) -> bool:
    """The only response shape treated as a confirmed submitted record."""

    return isinstance(payload, dict) and payload.get("code") == 0
