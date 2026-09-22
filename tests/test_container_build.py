from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_playwright_and_container_browser_are_pinned_together() -> None:
    requirements = (PROJECT_ROOT / "requirements.txt").read_text()
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()

    assert "playwright==1.60.0" in requirements
    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in dockerfile
    assert "python -m playwright install --with-deps chromium" in dockerfile
    assert "chromium-headless-shell" not in dockerfile
    assert "CHROMIUM_EXECUTABLE_PATH=" not in dockerfile
    assert 'chown -R bonski:bonski "$PLAYWRIGHT_BROWSERS_PATH"' in dockerfile


def test_compose_forwards_optional_playwright_download_configuration() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text()

    assert 'PLAYWRIGHT_DOWNLOAD_HOST: "${BONSKI_PLAYWRIGHT_DOWNLOAD_HOST:-}"' in compose
    assert (
        "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT: "
        '"${BONSKI_PLAYWRIGHT_DOWNLOAD_TIMEOUT:-120000}"'
    ) in compose


def test_default_compose_starts_local_development_without_vpn() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text()

    assert "  vpn:" not in compose
    assert "network_mode: service:vpn" not in compose
    assert '      - "127.0.0.1:8911:8080"' in compose
    assert 'DRY_RUN: "${DRY_RUN:-true}"' in compose


def test_compose_routes_bonskiboard_only_through_vpn() -> None:
    compose = (PROJECT_ROOT / "compose.vpn.yaml").read_text()
    auth_config = (PROJECT_ROOT / "gluetun-auth.toml").read_text()

    assert "image: ghcr.io/qdm12/gluetun:v3.41.3" in compose
    assert 'network_mode: service:vpn' in compose
    assert "      - NET_ADMIN" in compose
    assert "      - CHOWN" in compose
    assert "      - DAC_OVERRIDE" in compose
    assert '      - "8911:8080"' in compose
    assert "source: ./hk-3-IrisBh.conf" in compose
    assert "target: /gluetun/wireguard/wg0.conf" in compose
    assert "read_only: true" in compose
    assert "FIREWALL_ENABLED_DISABLING_IT_SHOOTS_YOU_IN_YOUR_FOOT: \"on\"" in compose
    assert "LOG_LEVEL: error" in compose
    assert "WIREGUARD_IMPLEMENTATION: userspace" in compose
    assert "PUBLICIP_FILE: /tmp/gluetun-public-ip" in compose
    assert "subnet: 192.168.250.0/24" in compose
    assert 'auth = "none"' in auth_config
    assert '"GET /v1/vpn/status"' in auth_config
    assert '"GET /v1/publicip/ip"' in auth_config
