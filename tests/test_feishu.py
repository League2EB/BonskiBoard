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


class FakeNavigatingPage(FakePage):
    def __init__(
        self,
        *,
        goto_side_effect: Optional[object] = None,
    ) -> None:
        self.goto = AsyncMock(side_effect=goto_side_effect)
        self.set_viewport_size = AsyncMock()
        self.close = AsyncMock()


class FakeContext:
    def __init__(self, events: list[str], page: FakePage) -> None:
        self.events = events
        self.page = page
        self.close = AsyncMock(side_effect=self._close)

    async def _close(self) -> None:
        self.events.append("context_close")

    async def new_page(self) -> FakePage:
        return self.page


class FakePageSequenceContext(FakeContext):
    def __init__(self, events: list[str], pages: list[FakeNavigatingPage]) -> None:
        super().__init__(events, pages[0])
        self.pages = pages
        self.new_page = AsyncMock(side_effect=pages)


class FakeBrowser:
    def __init__(
        self,
        events: list[str],
        context: FakeContext,
        *,
        version: object = "unknown",
    ) -> None:
        self.events = events
        self.context = context
        self.version = version
        self.context_options: dict[str, object] | None = None
        self.close = AsyncMock(side_effect=self._close)

    async def _close(self) -> None:
        self.events.append("browser_close")

    async def new_context(self, **kwargs: object) -> FakeContext:
        self.events.append("new_context")
        self.context_options = kwargs
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
        vpn_health_url="http://127.0.0.1:9999/",
        vpn_status_url="http://127.0.0.1:8000/v1/vpn/status",
        vpn_public_ip_url="http://127.0.0.1:8000/v1/publicip/ip",
        vpn_timeout_seconds=1.0,
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
    assert browser.context_options == {
        "accept_downloads": False,
        "viewport": feishu.FORM_VIEWPORTS[0],
        "locale": "zh-TW",
    }
    assert events.index("browser_close") < events.index("playwright_exit")


def test_confirmed_submission_invokes_submit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    submitter, browser, context, events = make_submitter(monkeypatch, dry_run=False)
    submitter._complete_form = AsyncMock()
    submission_events: list[str] = []

    async def record_vpn_check(_settings: object) -> str:
        submission_events.append("vpn_verified")
        return "8.8.8.8"

    async def record_submit(*_args: object) -> None:
        submission_events.append("feishu_submit")

    monkeypatch.setattr(feishu, "verify_vpn_egress", record_vpn_check)
    submitter._submit = AsyncMock(side_effect=record_submit)

    with caplog.at_level("INFO", logger="bonskiboard.feishu"):
        result = run_coroutine(
            submitter.submit(
                name="Test",
                board_number="123",
                ski_type="double",
                photos={},
                request_id="request-123",
            )
        )

    assert result.status == "submitted"
    assert submission_events == ["vpn_verified", "feishu_submit"]
    assert (
        "submission request_id=request-123 phase=vpn_verified "
        "vpn_status=running egress_ip=8.8.8.8"
    ) in caplog.text
    assert (
        "submission request_id=request-123 phase=feishu_submit_started"
    ) in caplog.text
    submitter._submit.assert_awaited_once()
    browser.close.assert_awaited_once()
    context.close.assert_not_awaited()
    assert events.index("browser_close") < events.index("playwright_exit")


def test_vpn_failure_prevents_feishu_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=False)
    submitter._complete_form = AsyncMock()
    submitter._submit = AsyncMock()
    vpn_error = feishu.ApiError(
        "vpn_unavailable",
        503,
        "VPN unavailable",
    )
    verify_vpn_egress = AsyncMock(side_effect=vpn_error)
    monkeypatch.setattr(feishu, "verify_vpn_egress", verify_vpn_egress)

    with pytest.raises(feishu.ApiError) as raised:
        run_coroutine(
            submitter.submit(
                name="Test",
                board_number="123",
                ski_type="double",
                photos={},
            )
        )

    assert raised.value is vpn_error
    verify_vpn_egress.assert_awaited_once_with(submitter.settings)
    submitter._submit.assert_not_awaited()


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


def test_form_load_retries_before_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    page = SimpleNamespace(goto=AsyncMock(), set_viewport_size=AsyncMock())
    expected_controls = SimpleNamespace()
    submitter._wait_for_current_form_ready = AsyncMock(
        side_effect=[
            feishu._FormLoadIncomplete("form_ready_timeout", 0),
            None,
        ]
    )
    submitter._preflight_form = AsyncMock(return_value=expected_controls)

    result = run_coroutine(submitter._load_and_preflight_form(page))

    assert result is expected_controls
    assert page.goto.await_count == 2
    assert page.set_viewport_size.await_args_list == [
        ((feishu.FORM_VIEWPORTS[0],), {}),
        ((feishu.FORM_VIEWPORTS[1],), {}),
    ]
    assert page.goto.await_args_list == [
        (
            (submitter.settings.feishu_form_url,),
            {
                "wait_until": "commit",
                "timeout": submitter._navigation_timeout_ms(),
            },
        ),
        (
            (submitter.settings.feishu_form_url,),
            {
                "wait_until": "commit",
                "timeout": submitter._navigation_timeout_ms(),
            },
        ),
    ]
    submitter._preflight_form.assert_awaited_once_with(
        page,
        attempt=2,
        viewport=feishu.FORM_VIEWPORTS[1],
    )
    assert submitter._wait_for_current_form_ready.await_args_list == [
        ((page,), {"attempt": 1}),
        ((page,), {"attempt": 2}),
    ]


def test_form_load_retries_with_fresh_page_and_closes_failed_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    first_page = FakeNavigatingPage(
        goto_side_effect=feishu.PlaywrightTimeoutError("navigation timed out")
    )
    second_page = FakeNavigatingPage()
    context = FakePageSequenceContext([], [first_page, second_page])
    expected_controls = SimpleNamespace()
    submitter._wait_for_current_form_ready = AsyncMock()
    submitter._preflight_form = AsyncMock(return_value=expected_controls)

    page, controls = run_coroutine(
        submitter._load_and_preflight_form(context=context)
    )

    assert page is second_page
    assert controls is expected_controls
    assert context.new_page.await_count == 2
    first_page.close.assert_awaited_once()
    second_page.close.assert_not_awaited()
    first_page.goto.assert_awaited_once_with(
        submitter.settings.feishu_form_url,
        wait_until="commit",
        timeout=submitter._navigation_timeout_ms(),
    )
    second_page.goto.assert_awaited_once_with(
        submitter.settings.feishu_form_url,
        wait_until="commit",
        timeout=submitter._navigation_timeout_ms(),
    )
    submitter._preflight_form.assert_awaited_once_with(
        second_page,
        attempt=2,
        viewport=feishu.FORM_VIEWPORTS[1],
    )


def test_navigation_failures_close_every_attempt_page_before_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    pages = [
        FakeNavigatingPage(
            goto_side_effect=feishu.PlaywrightTimeoutError("navigation timed out")
        ),
        FakeNavigatingPage(
            goto_side_effect=feishu.PlaywrightTimeoutError("navigation timed out")
        ),
    ]
    context = FakePageSequenceContext([], pages)

    with pytest.raises(feishu.ApiError) as raised:
        run_coroutine(submitter._load_and_preflight_form(context=context))

    assert raised.value.code == "form_unavailable"
    assert raised.value.status_code == 503
    assert context.new_page.await_count == feishu.FORM_LOAD_ATTEMPTS
    for page in pages:
        page.close.assert_awaited_once()


def test_browser_version_mismatch_stops_before_form_context_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    context = FakeContext(events, FakePage())
    browser = FakeBrowser(events, context, version="152.0.7977.82")
    manager = FakePlaywrightManager(events, browser)
    monkeypatch.setattr(feishu, "async_playwright", lambda: manager)
    submitter = FeishuSubmitter(
        SimpleNamespace(
            chromium_executable_path="/usr/bin/chromium",
            feishu_form_url="https://example.invalid/form",
            playwright_timeout_ms=1_000,
            request_timeout_seconds=100,
            dry_run=True,
        )
    )

    with pytest.raises(feishu.ApiError) as raised:
        run_coroutine(
            submitter.submit(
                name="Test",
                board_number="123",
                ski_type="single",
                photos={},
            )
        )

    assert raised.value.code == "form_unavailable"
    assert raised.value.status_code == 503
    assert "new_context" not in events
    browser.close.assert_awaited_once()


def test_browser_version_uses_playwright_pinned_chromium_major(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)

    assert submitter._is_expected_chromium_version("148.0.7778.96")
    assert submitter._is_expected_chromium_version("148.0.7778.0")
    assert submitter._is_expected_chromium_version("148.0.7778.97")
    assert not submitter._is_expected_chromium_version("149.0.7827.0")
    assert not submitter._is_expected_chromium_version("152.0.7977.82")
    assert not submitter._is_expected_chromium_version("Chromium")
    assert submitter._is_expected_chromium_version("unknown")


def test_load_stages_have_distinct_bounded_playwright_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    submitter.settings.playwright_timeout_ms = 80_000

    assert submitter._navigation_timeout_ms() == feishu.FORM_NAVIGATION_TIMEOUT_MS
    assert submitter._form_ready_timeout_ms(1) == 30_000
    assert submitter._form_ready_timeout_ms(2) == 10_000


def test_two_safe_load_attempts_leave_time_for_data_entry_and_submission() -> None:
    request_budget_ms = 100_000
    maximum_load_budget_ms = (
        feishu.FORM_LOAD_ATTEMPTS * feishu.FORM_NAVIGATION_TIMEOUT_MS
        + sum(feishu.FORM_READY_TIMEOUTS_MS)
    )

    assert maximum_load_budget_ms == 70_000
    assert maximum_load_budget_ms < request_budget_ms


def test_loaded_layout_error_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    page = SimpleNamespace(goto=AsyncMock(), set_viewport_size=AsyncMock())
    layout_error = feishu.ApiError(
        "form_layout_changed",
        502,
        "layout changed",
    )
    submitter._wait_for_current_form_ready = AsyncMock()
    submitter._preflight_form = AsyncMock(side_effect=layout_error)

    with pytest.raises(feishu.ApiError) as raised:
        run_coroutine(submitter._load_and_preflight_form(page))

    assert raised.value is layout_error
    assert page.goto.await_count == 1
    page.set_viewport_size.assert_awaited_once_with(feishu.FORM_VIEWPORTS[0])


def test_submit_error_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=False)
    submitter._complete_form = AsyncMock(return_value=FakeControl())
    submit_error = feishu.ApiError("submit_failed", 502, "submit failed")
    submitter._submit = AsyncMock(side_effect=submit_error)
    monkeypatch.setattr(
        feishu,
        "verify_vpn_egress",
        AsyncMock(return_value="8.8.8.8"),
    )

    with pytest.raises(feishu.ApiError) as raised:
        run_coroutine(
            submitter.submit(
                name="Test",
                board_number="123",
                ski_type="single",
                photos={},
            )
        )

    assert raised.value is submit_error
    submitter._complete_form.assert_awaited_once()
    submitter._submit.assert_awaited_once()


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


class FakeChangingLocator(FakeLocator):
    def __init__(self, snapshots: list[list[FakeControl]]) -> None:
        super().__init__([])
        self.snapshots = snapshots
        self.count_calls = 0

    async def count(self) -> int:
        snapshot_index = min(self.count_calls, len(self.snapshots) - 1)
        self.controls = self.snapshots[snapshot_index]
        self.count_calls += 1
        return len(self.controls)


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

    async def wait_for(self, *, state: str, timeout: int) -> None:
        assert state == "attached"
        assert timeout > 0
        self.events.append(f"ready:{self.selector}:{timeout}")
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
        self.set_viewport_size = AsyncMock()
        self.evaluate = AsyncMock(
            return_value={
                "ready_state": "loading",
                "body_text_length": 0,
                "html_length": 8_076,
                "resource_count": 12,
            }
        )

    def locator(self, selector: str) -> FakeReadyLocator:
        return self.locators[selector]


def test_contenteditable_control_is_resolved_inside_verified_field_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    contenteditable = FakeControl()
    selector = feishu.TEXT_CONTROL_SELECTOR
    page = FakeFieldPage(FakeLocator(
        [FakeControl()],
        {selector: FakeLocator([contenteditable])},
    ))

    result = run_coroutine(
        submitter._resolve_control(
            page,
            kind="name",
            selector=selector,
            container_fallback_selector='[contenteditable="true"]',
            legacy_selector="input:not([type='hidden']), textarea",
            terms=("姓名",),
        )
    )

    assert result is contenteditable


def test_text_control_falls_back_to_generic_contenteditable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    monkeypatch.setattr(feishu, "CONTROL_STABILITY_POLL_SECONDS", 0)
    contenteditable = FakeControl()
    page = FakeFieldPage(
        FakeLocator(
            [FakeControl()],
            {
                feishu.TEXT_CONTROL_SELECTOR: FakeLocator([]),
                '[contenteditable="true"]': FakeLocator([contenteditable]),
            },
        )
    )

    result = run_coroutine(
        submitter._resolve_control(
            page,
            kind="name",
            selector=feishu.TEXT_CONTROL_SELECTOR,
            container_fallback_selector='[contenteditable="true"]',
            legacy_selector="input:not([type='hidden']), textarea",
            terms=("姓名",),
        )
    )

    assert result is contenteditable


@pytest.mark.parametrize(
    "snapshots",
    (
        [[FakeControl(), FakeControl()], [FakeControl()], [FakeControl()]],
        [[], [FakeControl()], [FakeControl()]],
    ),
)
def test_control_resolution_waits_for_a_stable_unique_candidate(
    monkeypatch: pytest.MonkeyPatch,
    snapshots: list[list[FakeControl]],
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    monkeypatch.setattr(feishu, "CONTROL_STABILITY_POLL_SECONDS", 0)
    controls = FakeChangingLocator(snapshots)

    result = run_coroutine(
        submitter._require_single_control(
            controls,
            kind="name",
            require_visible=True,
        )
    )

    assert result is snapshots[-1][0]
    assert controls.count_calls == 3


def test_control_resolution_keeps_fail_closed_with_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    monkeypatch.setattr(feishu, "CONTROL_STABILITY_TIMEOUT_SECONDS", 0)
    controls = FakeChangingLocator([[FakeControl(), FakeControl()]])
    token = feishu._PREFLIGHT_DIAGNOSTICS.set(
        feishu._PreflightDiagnostics(
            attempt=2,
            viewport_width=325,
            viewport_height=793,
        )
    )

    try:
        with caplog.at_level("WARNING", logger="bonskiboard.feishu"):
            with pytest.raises(feishu.ApiError) as raised:
                run_coroutine(
                    submitter._require_single_control(
                        controls,
                        kind="name",
                        require_visible=True,
                    )
                )
    finally:
        feishu._PREFLIGHT_DIAGNOSTICS.reset(token)

    assert raised.value.code == "form_layout_changed"
    assert (
        "reason=control_guard kind=name candidate_count=2 visible_count=2 "
        "attempt=2 viewport=325x793"
    ) in caplog.text


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
    name_control = FakeControl()
    board_number_control = FakeControl()
    board_photo_control = FakeControl()
    card_photo_control = FakeControl()
    controls = [
        name_control,
        board_number_control,
        board_photo_control,
        card_photo_control,
    ]

    async def resolve_control(*_args: object, **_kwargs: object) -> FakeControl:
        events.append("resolve_control")
        return controls.pop(0)

    submitter._resolve_control = AsyncMock(side_effect=resolve_control)
    submitter._resolve_ski_type_control = AsyncMock(
        side_effect=lambda *_args: (events.append("resolve_ski_type"), FakeControl())[1]
    )
    submitter._resolve_submit_button = AsyncMock(
        side_effect=lambda *_args: (events.append("resolve_submit"), FakeControl())[1]
    )
    name_control.fill = AsyncMock(side_effect=lambda *_args: events.append("fill_name"))
    board_number_control.fill = AsyncMock(
        side_effect=lambda *_args: events.append("fill_board_number")
    )
    submitter._select_ski_type = AsyncMock(
        side_effect=lambda *_args, **_kwargs: events.append("select_ski_type")
    )
    submitter._upload_and_confirm = AsyncMock(
        side_effect=lambda _page, *, kind, **_kwargs: events.append(f"upload_{kind}")
    )
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
        event.removeprefix("ready:").rsplit(":", 1)[0]
        for event in events
        if event.startswith("ready:")
    } == set(CURRENT_FORM_CONTAINER_SELECTORS)
    assert all(
        events.index(
            f"ready:{selector}:{submitter._form_ready_timeout_ms(1)}"
        ) < events.index("resolve_control")
        for selector in CURRENT_FORM_CONTAINER_SELECTORS
    )
    preflight_complete = events.index("resolve_submit")
    assert all(
        preflight_complete < events.index(event)
        for event in (
            "fill_name",
            "fill_board_number",
            "select_ski_type",
            "upload_board_photo",
            "upload_card_photos",
        )
    )


class FakeUploadField:
    def __init__(
        self,
        labels: Optional[dict[str, FakeLocator]] = None,
        *,
        count: int = 1,
    ) -> None:
        self.labels = labels or {}
        self.field_count = count

    async def count(self) -> int:
        return self.field_count

    def get_by_text(self, filename: str, *, exact: bool) -> FakeLocator:
        assert exact
        return self.labels.get(filename, FakeLocator([]))


class FakeUploadPage:
    def __init__(self, fields: dict[str, FakeUploadField]) -> None:
        self.fields = fields

    def locator(self, selector: str) -> FakeUploadField:
        return self.fields[selector]


def make_upload_page(
    *,
    board_labels: Optional[dict[str, FakeLocator]] = None,
    card_labels: Optional[dict[str, FakeLocator]] = None,
    board_field_count: int = 1,
    card_field_count: int = 1,
) -> FakeUploadPage:
    return FakeUploadPage(
        {
            f"#field-item-{FIELD_IDS['board_photo']}": FakeUploadField(
                board_labels,
                count=board_field_count,
            ),
            f"#field-item-{FIELD_IDS['card_photos']}": FakeUploadField(
                card_labels,
                count=card_field_count,
            ),
        }
    )


def test_attachment_confirmation_accepts_replaced_file_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    control = FakeControl(file_count=0)
    page = make_upload_page(
        board_labels={"board_photo.jpg": FakeLocator([FakeControl()])}
    )

    run_coroutine(
        submitter._upload_and_confirm(
            page,
            control=control,
            kind="board_photo",
            files="/tmp/board_photo.jpg",
            filenames=("board_photo.jpg",),
        )
    )

    control.set_input_files.assert_awaited_once_with("/tmp/board_photo.jpg")
    control.evaluate.assert_not_awaited()


def test_attachment_input_error_is_classified_as_upload_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    control = FakeControl()
    control.set_input_files = AsyncMock(
        side_effect=feishu.PlaywrightError("input replaced")
    )
    page = make_upload_page()

    with pytest.raises(feishu.ApiError) as raised:
        run_coroutine(
            submitter._upload_and_confirm(
                page,
                control=control,
                kind="board_photo",
                files="/tmp/board_photo.jpg",
                filenames=("board_photo.jpg",),
            )
        )

    assert raised.value.code == "upload_failed"


def test_attachment_confirmation_requires_filenames_in_their_own_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    monkeypatch.setattr(feishu, "UPLOAD_CONFIRM_TIMEOUT_SECONDS", 0)
    page = make_upload_page(
        card_labels={"board_photo.jpg": FakeLocator([FakeControl()])}
    )

    with pytest.raises(feishu.ApiError) as raised:
        run_coroutine(
            submitter._wait_for_uploaded_files(
                page,
                kind="board_photo",
                filenames=("board_photo.jpg",),
            )
        )

    assert raised.value.code == "upload_failed"


@pytest.mark.parametrize(
    ("kind", "filenames", "field_count"),
    (
        ("board_photo", ("board_photo.jpg",), 0),
        ("board_photo", ("board_photo.jpg",), 2),
        (
            "card_photos",
            ("card_front_photo.jpg", "card_back_photo.jpg"),
            0,
        ),
        (
            "card_photos",
            ("card_front_photo.jpg", "card_back_photo.jpg"),
            2,
        ),
    ),
)
def test_attachment_confirmation_rejects_lost_or_duplicated_field(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    filenames: tuple[str, ...],
    field_count: int,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    monkeypatch.setattr(feishu, "UPLOAD_CONFIRM_TIMEOUT_SECONDS", 0)
    page = make_upload_page(
        **{
            (
                "board_field_count"
                if kind == "board_photo"
                else "card_field_count"
            ): field_count
        }
    )

    with pytest.raises(feishu.ApiError) as raised:
        run_coroutine(
            submitter._wait_for_uploaded_files(
                page,
                kind=kind,
                filenames=filenames,
            )
        )

    assert raised.value.code == "upload_failed"


@pytest.mark.parametrize(
    "labels",
    (
        {},
        {"board_photo.jpg": FakeLocator([FakeControl(visible=False)])},
        {"board_photo.jpg": FakeLocator([FakeControl(), FakeControl()])},
    ),
)
def test_attachment_confirmation_rejects_missing_invisible_or_duplicate_filename(
    monkeypatch: pytest.MonkeyPatch,
    labels: dict[str, FakeLocator],
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    monkeypatch.setattr(feishu, "UPLOAD_CONFIRM_TIMEOUT_SECONDS", 0)
    page = make_upload_page(board_labels=labels)

    with pytest.raises(feishu.ApiError) as raised:
        run_coroutine(
            submitter._wait_for_uploaded_files(
                page,
                kind="board_photo",
                filenames=("board_photo.jpg",),
            )
        )

    assert raised.value.code == "upload_failed"


@pytest.mark.parametrize(
    "labels",
    (
        {"card_back_photo.jpg": FakeLocator([FakeControl()])},
        {"card_front_photo.jpg": FakeLocator([FakeControl()])},
        {
            "card_front_photo.jpg": FakeLocator([FakeControl(visible=False)]),
            "card_back_photo.jpg": FakeLocator([FakeControl()]),
        },
        {
            "card_front_photo.jpg": FakeLocator([FakeControl()]),
            "card_back_photo.jpg": FakeLocator([FakeControl(), FakeControl()]),
        },
    ),
)
def test_attachment_confirmation_rejects_each_card_filename_failure(
    monkeypatch: pytest.MonkeyPatch,
    labels: dict[str, FakeLocator],
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    monkeypatch.setattr(feishu, "UPLOAD_CONFIRM_TIMEOUT_SECONDS", 0)
    page = make_upload_page(card_labels=labels)

    with pytest.raises(feishu.ApiError) as raised:
        run_coroutine(
            submitter._wait_for_uploaded_files(
                page,
                kind="card_photos",
                filenames=("card_front_photo.jpg", "card_back_photo.jpg"),
            )
        )

    assert raised.value.code == "upload_failed"


def test_attachment_confirmation_requires_both_card_filenames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    page = make_upload_page(
        card_labels={
            "card_front_photo.jpg": FakeLocator([FakeControl()]),
            "card_back_photo.jpg": FakeLocator([FakeControl()]),
        }
    )

    run_coroutine(
        submitter._wait_for_uploaded_files(
            page,
            kind="card_photos",
            filenames=("card_front_photo.jpg", "card_back_photo.jpg"),
        )
    )


def test_attachment_confirmation_logs_safe_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    monkeypatch.setattr(feishu, "UPLOAD_CONFIRM_TIMEOUT_SECONDS", 0)
    page = make_upload_page(board_field_count=0)
    token = feishu._PREFLIGHT_DIAGNOSTICS.set(
        feishu._PreflightDiagnostics(
            attempt=2,
            viewport_width=325,
            viewport_height=793,
        )
    )

    try:
        with caplog.at_level("WARNING", logger="bonskiboard.feishu"):
            with pytest.raises(feishu.ApiError):
                run_coroutine(
                    submitter._wait_for_uploaded_files(
                        page,
                        kind="board_photo",
                        filenames=("board_photo.jpg",),
                    )
                )
    finally:
        feishu._PREFLIGHT_DIAGNOSTICS.reset(token)

    assert "feishu_upload phase=failed" in caplog.text
    assert (
        "kind=board_photo field_count=0 candidate_count=0 visible_count=0 "
        "attempt=2 viewport=325x793"
    ) in caplog.text
    assert "board_photo.jpg" not in caplog.text


def test_upload_failure_prevents_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=False)
    upload_error = feishu.ApiError("upload_failed", 502, "upload failed")
    controls = feishu._PreflightedControls(
        name_input=FakeControl(),
        board_number_input=FakeControl(),
        ski_type_control=FakeControl(),
        board_photo_input=FakeControl(),
        card_photo_input=FakeControl(),
        submit_button=FakeControl(),
        attempt=2,
        viewport_width=325,
        viewport_height=793,
    )
    submitter._load_and_preflight_form = AsyncMock(return_value=controls)
    submitter._select_ski_type = AsyncMock()
    submitter._upload_and_confirm = AsyncMock(side_effect=upload_error)
    submitter._submit = AsyncMock()
    photos = {
        "board_photo": SimpleNamespace(path=Path("board_photo.jpg")),
        "card_front_photo": SimpleNamespace(path=Path("card_front_photo.jpg")),
        "card_back_photo": SimpleNamespace(path=Path("card_back_photo.jpg")),
    }

    with pytest.raises(feishu.ApiError) as raised:
        run_coroutine(
            submitter.submit(
                name="Test",
                board_number="123",
                ski_type="single",
                photos=photos,
            )
        )

    assert raised.value is upload_error
    submitter._upload_and_confirm.assert_awaited_once()
    submitter._submit.assert_not_awaited()


def test_current_form_readiness_timeout_retries_before_failing_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    events: list[str] = []
    page = FakeReadyPage(
        events,
        timeout_selector=f"#field-item-{FIELD_IDS['board_number']}",
    )
    submitter._resolve_control = AsyncMock()

    with caplog.at_level("WARNING", logger="bonskiboard.feishu"):
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

    assert raised.value.code == "form_unavailable"
    assert raised.value.status_code == 503
    assert page.goto.await_count == feishu.FORM_LOAD_ATTEMPTS
    assert page.set_viewport_size.await_count == feishu.FORM_LOAD_ATTEMPTS
    submitter._resolve_control.assert_not_awaited()
    timeout_selector = f"#field-item-{FIELD_IDS['board_number']}"
    assert (
        f"ready:{timeout_selector}:{submitter._form_ready_timeout_ms(1)}"
        in events
    )
    assert (
        f"ready:{timeout_selector}:{submitter._form_ready_timeout_ms(2)}"
        in events
    )
    assert (
        "ready_state=loading body_text_length=0 html_length=8076 "
        "resource_count=12"
    ) in caplog.text


@pytest.mark.parametrize("count", (0, 2))
def test_ski_type_option_absence_or_ambiguity_fails_closed(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    submitter, _, _, _ = make_submitter(monkeypatch, dry_run=True)
    page = FakeOptionPage(FakeLocator([FakeControl() for _ in range(count)]))

    with pytest.raises(feishu.ApiError) as raised:
        run_coroutine(submitter._click_expected_ski_type_option(page, "single"))

    assert raised.value.code == "form_layout_changed"
