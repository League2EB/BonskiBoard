"""Conservative anonymous browser automation for the fixed Feishu form."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .images import SanitizedPhoto
from .security import ApiError
from .settings import Settings


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
SKI_TYPE_CONTROL_SELECTOR = (
    '[data-form-control-interactive-root="true"], '
    "[role='combobox'], [role='button'], button, select, "
    "[aria-haspopup='listbox']"
)
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


@dataclass(frozen=True)
class SubmissionResult:
    status: str
    message: str


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
    ) -> SubmissionResult:
        try:
            async with async_playwright() as playwright:
                browser: Optional[Browser] = None
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
                    browser = await playwright.chromium.launch(**launch_options)
                    context = await browser.new_context(
                        accept_downloads=False,
                        viewport={"width": 1280, "height": 900},
                        locale="zh-TW",
                    )
                    page = await context.new_page()
                    page.set_default_timeout(self.settings.playwright_timeout_ms)
                    await self._complete_form(
                        page=page,
                        name=name,
                        board_number=board_number,
                        ski_type=ski_type,
                        photos=photos,
                    )
                    if self.settings.dry_run:
                        return SubmissionResult(
                            status="dry_run_complete",
                            message="檢查完成，未正式送出。",
                        )
                    await self._submit(page)
                    return SubmissionResult(status="submitted", message="申請已送出。")
                finally:
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
        page: Page,
        name: str,
        board_number: str,
        ski_type: str,
        photos: dict[str, SanitizedPhoto],
    ) -> None:
        await page.goto(
            self.settings.feishu_form_url,
            wait_until="domcontentloaded",
            timeout=self.settings.playwright_timeout_ms,
        )
        await page.wait_for_load_state("networkidle")
        await self._wait_for_current_form_ready(page)

        name_input = await self._resolve_control(
            page,
            kind="name",
            selector="[contenteditable='true'], input:not([type='hidden']), textarea",
            legacy_selector="input:not([type='hidden']), textarea",
            terms=TEXT_TERMS["name"],
        )
        board_number_input = await self._resolve_control(
            page,
            kind="board_number",
            selector="[contenteditable='true'], input:not([type='hidden']), textarea",
            legacy_selector="input:not([type='hidden']), textarea",
            terms=TEXT_TERMS["board_number"],
        )
        board_photo_input = await self._resolve_control(
            page,
            kind="board_photo",
            selector="input[type='file']",
            legacy_selector="input[type='file']",
            terms=PHOTO_TERMS["board_photo"],
        )
        card_photo_input = await self._resolve_control(
            page,
            kind="card_photos",
            selector="input[type='file']",
            legacy_selector="input[type='file']",
            terms=PHOTO_TERMS["card_photos"],
        )

        await name_input.fill(name)
        await board_number_input.fill(board_number)
        await self._select_ski_type(page, ski_type)
        await board_photo_input.set_input_files(str(photos["board_photo"].path))
        await card_photo_input.set_input_files(
            [
                str(photos["card_front_photo"].path),
                str(photos["card_back_photo"].path),
            ]
        )
        await self._require_attachment_file_count(board_photo_input, expected_count=1)
        await self._require_attachment_file_count(card_photo_input, expected_count=2)
        await self._wait_for_uploads(
            page,
            (
                photos["board_photo"].path.name,
                photos["card_front_photo"].path.name,
                photos["card_back_photo"].path.name,
            ),
        )

        await self._resolve_submit_button(page)

    async def _wait_for_current_form_ready(self, page: Page) -> None:
        """Wait for delayed current-layout fields without changing legacy fallback."""

        ski_type_container = page.locator(
            f"#field-item-{FIELD_IDS['ski_type']}"
        )
        if await ski_type_container.count() == 0:
            return
        try:
            await asyncio.gather(
                *(
                    page.locator(selector).wait_for(state="attached")
                    for selector in CURRENT_FORM_CONTAINER_SELECTORS
                )
            )
        except PlaywrightTimeoutError as exc:
            raise self._layout_changed("form_ready_guard") from exc

    async def _resolve_control(
        self,
        page: Page,
        *,
        kind: str,
        selector: str,
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
        raise self._layout_changed("control_legacy_guard")

    async def _require_single_control(
        self,
        controls: Locator,
        *,
        kind: str,
        require_visible: bool,
    ) -> Locator:
        matches = []
        for index in range(await controls.count()):
            control = controls.nth(index)
            if not require_visible or await control.is_visible():
                matches.append(control)
        if len(matches) != 1:
            raise self._layout_changed("control_guard")
        return matches[0]

    async def _select_ski_type(self, page: Page, ski_type: str) -> None:
        if ski_type not in SKI_TYPE_CHOICES:
            raise self._layout_changed("ski_type_value_guard")

        field = page.locator(f"#field-item-{FIELD_IDS['ski_type']}")
        if await field.count() != 1:
            raise self._layout_changed("ski_type_control_guard")
        control = await self._require_single_control(
            field.locator(SKI_TYPE_CONTROL_SELECTOR),
            kind="ski_type",
            require_visible=True,
        )
        await control.click()
        await self._click_expected_ski_type_option(page, ski_type)

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

    async def _require_attachment_file_count(
        self, control: Locator, *, expected_count: int
    ) -> None:
        try:
            file_count = await control.evaluate(
                """(element) => (
                    element instanceof HTMLInputElement
                    && element.type === 'file'
                    && element.files
                    ? element.files.length
                    : null
                )"""
            )
        except Exception as exc:
            raise self._layout_changed("attachment_file_count_guard") from exc
        if file_count != expected_count:
            raise self._layout_changed("attachment_file_count_guard")

    async def _wait_for_uploads(self, page: Page, filenames: tuple[str, ...]) -> None:
        try:
            for filename in filenames:
                await page.get_by_text(filename, exact=True).wait_for(state="visible")
        except PlaywrightTimeoutError as exc:
            raise ApiError(
                "upload_failed",
                502,
                "照片上傳未完成，請稍後再試。",
            ) from exc

    async def _submit(self, page: Page) -> None:
        submit_button = await self._resolve_submit_button(page)
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
        if await submit_button.count() != 1:
            raise self._layout_changed("submit_button_guard")
        try:
            await submit_button.wait_for(state="visible")
        except PlaywrightTimeoutError as exc:
            raise self._layout_changed("submit_button_visibility_guard") from exc
        return submit_button

    @staticmethod
    def _layout_changed(reason: str) -> ApiError:
        LOGGER.warning("feishu_layout_guard reason=%s", reason)
        return ApiError(
            "form_layout_changed",
            502,
            "飛書表單已變更，為避免誤送，本次申請已停止。",
        )


def is_successful_submit_response(payload: object) -> bool:
    """The only response shape treated as a confirmed submitted record."""

    return isinstance(payload, dict) and payload.get("code") == 0
