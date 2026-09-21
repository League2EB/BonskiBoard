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
