import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock

import pytest

import app.feishu as feishu
from app.feishu import (
    CURRENT_FORM_CONTAINER_SELECTORS,
    FIELD_IDS,
    SKI_TYPE_CHOICES,
    SKI_TYPE_CONTROL_SELECTOR,
    FeishuSubmitter,
    is_successful_submit_response,
)


class FakePage:
    def set_default_timeout(self, timeout: int) -> None:
        self.timeout = timeout


class FakeContext:
    def __init__(self, events: list[str], page: FakePage) -> None:
        self.events = events
        self.page = page
        self.close = AsyncMock(side_effect=self._close)

    async def _close(self) -> None:
        self.events.append("context_close")

    async def new_page(self) -> FakePage:
        return self.page


class FakeBrowser:
    def __init__(self, events: list[str], context: FakeContext) -> None:
        self.events = events
        self.context = context
        self.close = AsyncMock(side_effect=self._close)

    async def _close(self) -> None:
        self.events.append("browser_close")

    async def new_context(self, **kwargs: object) -> FakeContext:
        self.events.append("new_context")
        return self.context


class FakePlaywrightManager:
    def __init__(self, events: list[str], browser: FakeBrowser) -> None:
        self.events = events
        self.browser = browser
        self.playwright = SimpleNamespace(
            chromium=SimpleNamespace(launch=AsyncMock(return_value=browser))
        )

    async def __aenter__(self) -> SimpleNamespace:
        self.events.append("playwright_enter")
        return self.playwright

    async def __aexit__(self, *args: object) -> None:
        self.events.append("playwright_exit")


def make_submitter(
    monkeypatch: pytest.MonkeyPatch, *, dry_run: bool
) -> tuple[FeishuSubmitter, FakeBrowser, FakeContext, list[str]]:
    events: list[str] = []
    page = FakePage()
    context = FakeContext(events, page)
    browser = FakeBrowser(events, context)
    manager = FakePlaywrightManager(events, browser)
    monkeypatch.setattr(feishu, "async_playwright", lambda: manager)
    settings = SimpleNamespace(
        chromium_executable_path=None,
        feishu_form_url="https://example.invalid/form",
        playwright_timeout_ms=1_000,
        dry_run=dry_run,
    )
    return FeishuSubmitter(settings), browser, context, events


def run_coroutine(coroutine: object) -> object:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


def test_dry_run_closes_browser_before_playwright_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, browser, context, events = make_submitter(monkeypatch, dry_run=True)
    submitter._complete_form = AsyncMock()

    result = run_coroutine(
        submitter.submit(name="Test", board_number="123", ski_type="single", photos={})
    )

    assert result.status == "dry_run_complete"
    browser.close.assert_awaited_once()
    context.close.assert_not_awaited()
    assert events.index("browser_close") < events.index("playwright_exit")


def test_confirmed_submission_invokes_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, browser, context, events = make_submitter(monkeypatch, dry_run=False)
    submitter._complete_form = AsyncMock()
    submitter._submit = AsyncMock()

    result = run_coroutine(
        submitter.submit(name="Test", board_number="123", ski_type="double", photos={})
    )

    assert result.status == "submitted"
    submitter._submit.assert_awaited_once()
    browser.close.assert_awaited_once()
    context.close.assert_not_awaited()
    assert events.index("browser_close") < events.index("playwright_exit")


def test_form_api_error_survives_browser_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, browser, context, events = make_submitter(monkeypatch, dry_run=True)
    form_error = feishu.ApiError("upload_failed", 502, "upload failed")
    submitter._complete_form = AsyncMock(side_effect=form_error)

    with pytest.raises(feishu.ApiError) as raised:
        run_coroutine(
            submitter.submit(
                name="Test",
                board_number="123",
                ski_type="single",
                photos={},
            )
        )

    assert raised.value is form_error
    browser.close.assert_awaited_once()
    context.close.assert_not_awaited()
    assert events.index("browser_close") < events.index("playwright_exit")


def test_only_explicit_feishu_success_code_is_accepted() -> None:
    assert is_successful_submit_response({"code": 0, "data": {"canSubmitAgain": True}})
    assert not is_successful_submit_response({"code": 1})
    assert not is_successful_submit_response({"data": {"code": 0}})
    assert not is_successful_submit_response([])
    assert not is_successful_submit_response(None)


class FakeControl:
    def __init__(
        self, *, visible: bool = True, file_count: Optional[int] = None
    ) -> None:
        self.visible = visible
        self.click = AsyncMock()
        self.evaluate = AsyncMock(return_value=file_count)
        self.fill = AsyncMock()
        self.set_input_files = AsyncMock()

    async def is_visible(self) -> bool:
        return self.visible


class FakeLocator:
    def __init__(
        self,
        controls: list[FakeControl],
        children: Optional[dict[str, "FakeLocator"]] = None,
    ) -> None:
        self.controls = controls
        self.children = children or {}

    async def count(self) -> int:
        return len(self.controls)

    def nth(self, index: int) -> FakeControl:
        return self.controls[index]

    def locator(self, selector: str) -> "FakeLocator":
        return self.children[selector]


class FakeFieldPage:
    def __init__(self, field: FakeLocator) -> None:
        self.field = field

    def locator(self, selector: str) -> FakeLocator:
        assert selector == f"#field-item-{FIELD_IDS['name']}"
        return self.field


class FakeOptionPage:
    def __init__(self, options: FakeLocator) -> None:
        self.options = options

    def get_by_text(self, text: str, *, exact: bool) -> FakeLocator:
        assert text == SKI_TYPE_CHOICES["single"][0]
        assert exact
        return self.options


class FakeSkiTypePage:
    def __init__(self, field: FakeLocator) -> None:
        self.field = field

    def locator(self, selector: str) -> FakeLocator:
        assert selector == f"#field-item-{FIELD_IDS['ski_type']}"
        return self.field


class FakeReadyLocator:
    def __init__(
        self,
        selector: str,
        events: list[str],
        *,
        should_timeout: bool = False,
    ) -> None:
        self.selector = selector
        self.events = events
        self.should_timeout = should_timeout

    async def count(self) -> int:
        return 1

    async def wait_for(self, *, state: str) -> None:
        assert state == "attached"
        self.events.append(f"ready:{self.selector}")
        if self.should_timeout:
            raise feishu.PlaywrightTimeoutError("timed out")


class FakeReadyPage:
    def __init__(self, events: list[str], *, timeout_selector: Optional[str] = None) -> None:
        self.events = events
        self.locators = {
            selector: FakeReadyLocator(
                selector,
                events,
                should_timeout=selector == timeout_selector,
            )
            for selector in CURRENT_FORM_CONTAINER_SELECTORS
        }
        self.goto = AsyncMock()
        self.wait_for_load_state = AsyncMock()

    def locator(self, selector: str) -> FakeReadyLocator:
        return self.locators[selector]


def test_contenteditable_control_is_resolved_inside_verified_field_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    contenteditable = FakeControl()
    selector = "[contenteditable='true'], input:not([type='hidden']), textarea"
    page = FakeFieldPage(FakeLocator(
        [FakeControl()],
        {selector: FakeLocator([contenteditable])},
    ))

    result = run_coroutine(
        submitter._resolve_control(
            page,
            kind="name",
            selector=selector,
            legacy_selector="input:not([type='hidden']), textarea",
            terms=("姓名",),
        )
    )

    assert result is contenteditable


def test_ski_type_mapping_clicks_only_the_expected_unique_visible_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    option = FakeControl()

    run_coroutine(
        submitter._click_expected_ski_type_option(
            FakeOptionPage(FakeLocator([option])),
            "single",
        )
    )

    option.click.assert_awaited_once()
    assert SKI_TYPE_CHOICES == {
        "single": ("单板", "optHOXFuMJ"),
        "double": ("双板", "optFPiQW0g"),
    }


def test_ski_type_select_uses_verified_interactive_root_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    control = FakeControl()
    page = FakeSkiTypePage(
        FakeLocator(
            [FakeControl()],
            {SKI_TYPE_CONTROL_SELECTOR: FakeLocator([control])},
        )
    )
    submitter._click_expected_ski_type_option = AsyncMock()

    run_coroutine(submitter._select_ski_type(page, "single"))

    assert '[data-form-control-interactive-root="true"]' in SKI_TYPE_CONTROL_SELECTOR
    control.click.assert_awaited_once()
    submitter._click_expected_ski_type_option.assert_awaited_once_with(page, "single")


def test_current_form_readiness_waits_for_all_containers_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    events: list[str] = []
    page = FakeReadyPage(events)
    controls = [
        FakeControl(),
        FakeControl(),
        FakeControl(file_count=1),
        FakeControl(file_count=2),
    ]

    async def resolve_control(*_args: object, **_kwargs: object) -> FakeControl:
        events.append("resolve_control")
        return controls.pop(0)

    submitter._resolve_control = AsyncMock(side_effect=resolve_control)
    submitter._select_ski_type = AsyncMock()
    submitter._wait_for_uploads = AsyncMock()
    submitter._resolve_submit_button = AsyncMock()
    photos = {
        "board_photo": SimpleNamespace(path=Path("board.jpg")),
        "card_front_photo": SimpleNamespace(path=Path("front.jpg")),
        "card_back_photo": SimpleNamespace(path=Path("back.jpg")),
    }

    run_coroutine(
        submitter._complete_form(
            page=page,
            name="Test",
            board_number="123",
            ski_type="single",
            photos=photos,
        )
    )

    assert {
        event.removeprefix("ready:")
        for event in events
        if event.startswith("ready:")
    } == set(CURRENT_FORM_CONTAINER_SELECTORS)
    assert all(
        events.index(f"ready:{selector}") < events.index("resolve_control")
        for selector in CURRENT_FORM_CONTAINER_SELECTORS
    )


def test_attachment_file_counts_require_exact_uploaded_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    board_photo = FakeControl(file_count=1)
    card_photos = FakeControl(file_count=2)

    run_coroutine(
        submitter._require_attachment_file_count(board_photo, expected_count=1)
    )
    run_coroutine(
        submitter._require_attachment_file_count(card_photos, expected_count=2)
    )

    board_photo.evaluate.assert_awaited_once()
    card_photos.evaluate.assert_awaited_once()
    assert "HTMLInputElement" in board_photo.evaluate.await_args.args[0]


@pytest.mark.parametrize("file_count", (0, 3, None))
def test_attachment_file_count_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch, file_count: Optional[int]
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)

    with pytest.raises(feishu.ApiError) as raised:
        run_coroutine(
            submitter._require_attachment_file_count(
                FakeControl(file_count=file_count),
                expected_count=1,
            )
        )

    assert raised.value.code == "form_layout_changed"


def test_current_form_readiness_timeout_fails_closed_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    page = FakeReadyPage(
        [],
        timeout_selector=f"#field-item-{FIELD_IDS['board_number']}",
    )
    submitter._resolve_control = AsyncMock()

    with pytest.raises(feishu.ApiError) as raised:
        run_coroutine(
            submitter._complete_form(
                page=page,
                name="Test",
                board_number="123",
                ski_type="single",
                photos={},
            )
        )

    assert raised.value.code == "form_layout_changed"
    submitter._resolve_control.assert_not_awaited()


@pytest.mark.parametrize("count", (0, 2))
def test_ski_type_option_absence_or_ambiguity_fails_closed(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    page = FakeOptionPage(FakeLocator([FakeControl() for _ in range(count)]))

    with pytest.raises(feishu.ApiError) as raised:
        run_coroutine(submitter._click_expected_ski_type_option(page, "single"))

    assert raised.value.code == "form_layout_changed"
