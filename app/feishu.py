"""Conservative anonymous browser automation for the fixed Feishu form."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

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
    "board_photo": "fldBBvxZS1",
    "card_photos": "fldP0AVcVb",
}

TEXT_TERMS = {
    "name": ("姓名", "name"),
    "board_number": ("雪板編號", "雪板编号", "雪板號", "雪板号", "板號", "board"),
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
        photos: dict[str, SanitizedPhoto],
    ) -> SubmissionResult:
        browser: Browser | None = None
        context: Any | None = None
        try:
            async with async_playwright() as playwright:
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
                    photos=photos,
                )
                if self.settings.dry_run:
                    return SubmissionResult(
                        status="dry_run_complete",
                        message="已完成填寫與附件上傳檢查，未建立正式申請。",
                    )
                await self._submit(page)
                return SubmissionResult(status="submitted", message="申請已送出。")
        except ApiError:
            raise
        except PlaywrightTimeoutError as exc:
            raise ApiError(
                "submit_timeout",
                504,
                "飛書表單等待逾時，請稍後再試。",
            ) from exc
        except Exception as exc:
            raise ApiError(
                "submit_failed",
                502,
                "飛書表單目前無法完成送出，請稍後再試。",
            ) from exc
        finally:
            if context:
                await context.close()
            if browser:
                await browser.close()

    async def _complete_form(
        self,
        *,
        page: Page,
        name: str,
        board_number: str,
        photos: dict[str, SanitizedPhoto],
    ) -> None:
        await page.goto(
            self.settings.feishu_form_url,
            wait_until="domcontentloaded",
            timeout=self.settings.playwright_timeout_ms,
        )
        await page.wait_for_load_state("networkidle")

        name_input = await self._resolve_control(
            page,
            kind="name",
            selector="input:not([type='hidden']), textarea",
            terms=TEXT_TERMS["name"],
        )
        board_number_input = await self._resolve_control(
            page,
            kind="board_number",
            selector="input:not([type='hidden']), textarea",
            terms=TEXT_TERMS["board_number"],
        )
        board_photo_input = await self._resolve_control(
            page,
            kind="board_photo",
            selector="input[type='file']",
            terms=PHOTO_TERMS["board_photo"],
        )
        card_photo_input = await self._resolve_control(
            page,
            kind="card_photos",
            selector="input[type='file']",
            terms=PHOTO_TERMS["card_photos"],
        )
        # The form must still expose the expected single and multi-upload controls.
        if await board_photo_input.get_attribute("multiple") is not None:
            raise self._layout_changed()
        if await card_photo_input.get_attribute("multiple") is None:
            raise self._layout_changed()
        if not await self._has_default_choice(page):
            raise self._layout_changed()

        await name_input.fill(name)
        await board_number_input.fill(board_number)
        await board_photo_input.set_input_files(str(photos["board_photo"].path))
        await card_photo_input.set_input_files(
            [
                str(photos["card_front_photo"].path),
                str(photos["card_back_photo"].path),
            ]
        )
        await self._wait_for_uploads(
            page,
            (
                photos["board_photo"].path.name,
                photos["card_front_photo"].path.name,
                photos["card_back_photo"].path.name,
            ),
        )

        submit_button = page.get_by_role(
            "button",
            name=re.compile(r"^(送出|提交|submit)$", re.IGNORECASE),
        )
        if await submit_button.count() != 1:
            raise self._layout_changed()
        await submit_button.wait_for(state="visible")

    async def _resolve_control(
        self,
        page: Page,
        *,
        kind: str,
        selector: str,
        terms: tuple[str, ...],
    ) -> Locator:
        """Resolve a single target by its visible local field context.

        An exposed historical field ID is only used to narrow a matching,
        labelled field. It is never treated as an API contract by itself.
        """

        controls = page.locator(selector)
        count = await controls.count()
        candidates: list[tuple[Locator, str]] = []
        for index in range(count):
            control = controls.nth(index)
            if selector != "input[type='file']" and not await control.is_visible():
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

        # Diagnostic fallback: only accept a uniquely exposed known field ID.
        field_id = FIELD_IDS[kind]
        id_scoped = page.locator(
            f"[data-field-id='{field_id}'] {selector}, "
            f"[data-fieldid='{field_id}'] {selector}, "
            f"[data-field_id='{field_id}'] {selector}, "
            f"#{field_id} {selector}"
        )
        visible_matches = [
            id_scoped.nth(index)
            for index in range(await id_scoped.count())
            if selector == "input[type='file']"
            or await id_scoped.nth(index).is_visible()
        ]
        if len(visible_matches) == 1:
            return visible_matches[0]
        raise self._layout_changed()

    async def _has_default_choice(self, page: Page) -> bool:
        native_selected = page.locator("input[type='radio']:checked")
        accessible_selected = page.locator("[role='radio'][aria-checked='true']")
        return (await native_selected.count()) > 0 or (await accessible_selected.count()) > 0

    async def _wait_for_uploads(self, page: Page, filenames: tuple[str, ...]) -> None:
        try:
            for filename in filenames:
                await page.get_by_text(filename, exact=True).wait_for(state="visible")
        except PlaywrightTimeoutError as exc:
            raise ApiError(
                "upload_failed",
                502,
                "至少一張照片未完成上傳，請稍後再試。",
            ) from exc

    async def _submit(self, page: Page) -> None:
        submit_button = page.get_by_role(
            "button",
            name=re.compile(r"^(送出|提交|submit)$", re.IGNORECASE),
        )
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
                    "飛書未確認申請送出，請稍後再試。",
                )
            try:
                payload = await response.json()
            except Exception as exc:
                raise ApiError(
                    "submit_failed",
                    502,
                    "飛書未回傳可驗證的送出結果。",
                ) from exc
            if not is_successful_submit_response(payload):
                raise ApiError(
                    "submit_failed",
                    502,
                    "飛書未確認申請送出，請稍後再試。",
                )
        except PlaywrightTimeoutError as exc:
            raise ApiError(
                "submit_timeout",
                504,
                "等待飛書確認送出逾時，請稍後再試。",
            ) from exc

    @staticmethod
    def _layout_changed() -> ApiError:
        return ApiError(
            "form_layout_changed",
            502,
            "飛書表單欄位已變更，為避免錯誤送出，本次申請已停止。",
        )


def is_successful_submit_response(payload: object) -> bool:
    """The only response shape treated as a confirmed submitted record."""

    return isinstance(payload, dict) and payload.get("code") == 0
