"""Fail-closed local verification of the Gluetun VPN egress path."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from .security import ApiError
from .settings import Settings


def _vpn_unavailable() -> ApiError:
    return ApiError(
        "vpn_unavailable",
        503,
        "VPN 連線暫時無法確認，請稍後再試。",
    )


def _get_json(url: str, timeout_seconds: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise ValueError("VPN control response is not an object")
    return payload


def _check_health(url: str, timeout_seconds: float) -> None:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        if response.getcode() != 200:
            raise ValueError("VPN health endpoint returned a non-200 response")


async def _get_control_json(url: str, timeout_seconds: float) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_get_json, url, timeout_seconds),
            timeout=timeout_seconds,
        )
    except (
        asyncio.TimeoutError,
        OSError,
        URLError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise _vpn_unavailable() from exc


async def _check_vpn_health(url: str, timeout_seconds: float) -> None:
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_check_health, url, timeout_seconds),
            timeout=timeout_seconds,
        )
    except (asyncio.TimeoutError, OSError, URLError, ValueError) as exc:
        raise _vpn_unavailable() from exc


def _global_ip(value: object) -> str:
    if not isinstance(value, str):
        raise _vpn_unavailable()
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise _vpn_unavailable() from exc
    if not address.is_global or any(
        (
            address.is_private,
            address.is_loopback,
            address.is_reserved,
            address.is_unspecified,
            address.is_multicast,
            address.is_link_local,
        )
    ):
        raise _vpn_unavailable()
    return str(address)


async def verify_vpn_egress(settings: Settings) -> str:
    """Return a verified public egress IP or stop submission before its click."""

    timeout_seconds = settings.vpn_timeout_seconds
    if timeout_seconds <= 0:
        raise _vpn_unavailable()
    await _check_vpn_health(settings.vpn_health_url, timeout_seconds)
    status = await _get_control_json(settings.vpn_status_url, timeout_seconds)
    if status.get("status") != "running":
        raise _vpn_unavailable()
    public_ip = await _get_control_json(
        settings.vpn_public_ip_url,
        timeout_seconds,
    )
    return _global_ip(public_ip.get("public_ip"))
