from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.security import ApiError
import app.vpn as vpn


def run_coroutine(coroutine: object) -> object:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


def test_verify_vpn_egress_requires_healthy_running_public_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        vpn_health_url="http://127.0.0.1:9999/",
        vpn_status_url="http://127.0.0.1:8000/v1/vpn/status",
        vpn_public_ip_url="http://127.0.0.1:8000/v1/publicip/ip",
        vpn_timeout_seconds=1.0,
    )
    health = AsyncMock()
    responses = AsyncMock(
        side_effect=[
            {"status": "running"},
            {"public_ip": "8.8.8.8"},
        ]
    )
    monkeypatch.setattr(vpn, "_check_vpn_health", health)
    monkeypatch.setattr(vpn, "_get_control_json", responses)

    egress_ip = run_coroutine(vpn.verify_vpn_egress(settings))

    assert egress_ip == "8.8.8.8"
    health.assert_awaited_once_with(settings.vpn_health_url, 1.0)
    assert responses.await_args_list == [
        ((settings.vpn_status_url, 1.0), {}),
        ((settings.vpn_public_ip_url, 1.0), {}),
    ]


def test_verify_vpn_egress_fails_closed_when_vpn_is_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        vpn_health_url="http://127.0.0.1:9999/",
        vpn_status_url="http://127.0.0.1:8000/v1/vpn/status",
        vpn_public_ip_url="http://127.0.0.1:8000/v1/publicip/ip",
        vpn_timeout_seconds=1.0,
    )
    monkeypatch.setattr(vpn, "_check_vpn_health", AsyncMock())
    control = AsyncMock(return_value={"status": "stopped"})
    monkeypatch.setattr(vpn, "_get_control_json", control)

    with pytest.raises(ApiError) as raised:
        run_coroutine(vpn.verify_vpn_egress(settings))

    assert raised.value.code == "vpn_unavailable"
    assert raised.value.status_code == 503
    control.assert_awaited_once_with(settings.vpn_status_url, 1.0)
