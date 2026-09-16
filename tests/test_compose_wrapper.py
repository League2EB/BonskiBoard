from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_SCRIPT = PROJECT_ROOT / "scripts" / "compose.sh"
LEGACY_SCRIPT = PROJECT_ROOT / "scripts" / "orbstack-compose.sh"


FAKE_DOCKER = """#!/bin/sh
set -eu

printf '%s\\n' "$*" >> "$TRACE_FILE"

if [ "$1" = "compose" ] && [ "${2:-}" = "version" ]; then
    exit "${COMPOSE_VERSION_STATUS:-0}"
fi

printf '%s\\n' "${DOCKER_CONFIG-}" > "$CAPTURE_DOCKER_CONFIG"
printf '%s\\n' "${DOCKER_HOST-}" > "$CAPTURE_DOCKER_HOST"
if [ -n "${DOCKER_CONFIG-}" ] && [ -f "$DOCKER_CONFIG/config.json" ]; then
    cat "$DOCKER_CONFIG/config.json" > "$CAPTURE_CONFIG_JSON"
fi

exit "${COMPOSE_STATUS:-0}"
"""


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def wrapper_environment(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()

    environment = os.environ.copy()
    for variable in (
        "DOCKER_BIN",
        "DOCKER_CONFIG",
        "DOCKER_HOST",
        "BONSKI_DOCKER_CONFIG_MODE",
        "COMPOSE_STATUS",
        "COMPOSE_VERSION_STATUS",
    ):
        environment.pop(variable, None)
    environment.update(
        {
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "TRACE_FILE": str(tmp_path / "trace"),
            "CAPTURE_DOCKER_CONFIG": str(tmp_path / "docker-config"),
            "CAPTURE_DOCKER_HOST": str(tmp_path / "docker-host"),
            "CAPTURE_CONFIG_JSON": str(tmp_path / "config-json"),
        }
    )
    return environment


def run_wrapper(
    script: Path,
    environment: dict[str, str],
    *arguments: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(script), *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def fake_docker(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_executable(path, FAKE_DOCKER)


def trace(environment: dict[str, str]) -> list[str]:
    return Path(environment["TRACE_FILE"]).read_text().splitlines()


def utility_path(tmp_path: Path, *names: str) -> str:
    utilities = tmp_path / "utilities"
    utilities.mkdir()
    for name in names:
        source = shutil.which(name)
        if source is None:
            pytest.skip(f"{name} is required to test the shell wrapper")
        (utilities / name).symlink_to(source)
    return str(utilities)


def test_uses_docker_from_path_and_forwards_arguments(
    tmp_path: Path, wrapper_environment: dict[str, str]
) -> None:
    bin_dir = tmp_path / "bin"
    fake_docker(bin_dir / "docker")
    wrapper_environment["PATH"] = str(bin_dir)

    result = run_wrapper(COMPOSE_SCRIPT, wrapper_environment, "up", "-d", "--build")

    assert result.returncode == 0, result.stderr
    assert trace(wrapper_environment) == ["compose version", "compose up -d --build"]


def test_explicit_docker_bin_overrides_path(
    tmp_path: Path, wrapper_environment: dict[str, str]
) -> None:
    bin_dir = tmp_path / "bin"
    fake_docker(bin_dir / "docker")
    selected_docker = tmp_path / "selected-docker"
    fake_docker(selected_docker)
    wrapper_environment.update(
        {
            "PATH": str(bin_dir),
            "DOCKER_BIN": str(selected_docker),
        }
    )

    result = run_wrapper(COMPOSE_SCRIPT, wrapper_environment, "config")

    assert result.returncode == 0, result.stderr
    assert trace(wrapper_environment) == ["compose version", "compose config"]


def test_invalid_explicit_docker_bin_fails_without_fallback(
    tmp_path: Path, wrapper_environment: dict[str, str]
) -> None:
    bin_dir = tmp_path / "bin"
    fake_docker(bin_dir / "docker")
    wrapper_environment.update(
        {
            "PATH": str(bin_dir),
            "DOCKER_BIN": "missing-docker",
        }
    )

    result = run_wrapper(COMPOSE_SCRIPT, wrapper_environment, "config")

    assert result.returncode == 1
    assert "DOCKER_BIN does not resolve" in result.stderr
    assert not Path(wrapper_environment["TRACE_FILE"]).exists()


def test_falls_back_to_local_docker(
    wrapper_environment: dict[str, str]
) -> None:
    home = Path(wrapper_environment["HOME"])
    local_docker = home / ".local" / "bin" / "docker"
    fake_docker(local_docker)
    wrapper_environment["PATH"] = str(home / "empty-path")

    result = run_wrapper(COMPOSE_SCRIPT, wrapper_environment, "config")

    assert result.returncode == 0, result.stderr
    assert trace(wrapper_environment) == ["compose version", "compose config"]
    assert Path(wrapper_environment["CAPTURE_DOCKER_HOST"]).read_text() == "\n"


def test_falls_back_to_orbstack_via_local_symlink(
    tmp_path: Path, wrapper_environment: dict[str, str]
) -> None:
    with tempfile.TemporaryDirectory(prefix="bonski-compose-") as directory:
        home = Path(directory) / "home"
        home.mkdir()
        wrapper_environment["HOME"] = str(home)
        orbstack_docker = home / ".orbstack" / "bin" / "docker"
        fake_docker(orbstack_docker)
        local_docker = home / ".local" / "bin" / "docker"
        local_docker.parent.mkdir(parents=True)
        local_docker.symlink_to(orbstack_docker)

        socket_path = home / ".orbstack" / "run" / "docker.sock"
        socket_path.parent.mkdir(parents=True)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(socket_path))
            wrapper_environment["PATH"] = utility_path(tmp_path, "readlink")

            result = run_wrapper(COMPOSE_SCRIPT, wrapper_environment, "config")

    assert result.returncode == 0, result.stderr
    assert Path(wrapper_environment["CAPTURE_DOCKER_HOST"]).read_text().strip() == (
        f"unix://{socket_path}"
    )


def test_existing_docker_host_is_preserved_for_orbstack(
    tmp_path: Path, wrapper_environment: dict[str, str]
) -> None:
    home = Path(wrapper_environment["HOME"])
    orbstack_docker = home / ".orbstack" / "bin" / "docker"
    fake_docker(orbstack_docker)
    wrapper_environment.update(
        {
            "PATH": utility_path(tmp_path, "readlink"),
            "DOCKER_HOST": "tcp://docker.example.test:2376",
        }
    )

    result = run_wrapper(COMPOSE_SCRIPT, wrapper_environment, "config")

    assert result.returncode == 0, result.stderr
    assert Path(wrapper_environment["CAPTURE_DOCKER_HOST"]).read_text().strip() == (
        "tcp://docker.example.test:2376"
    )


def test_reports_missing_docker(
    tmp_path: Path, wrapper_environment: dict[str, str]
) -> None:
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    wrapper_environment["PATH"] = str(empty_path)

    result = run_wrapper(COMPOSE_SCRIPT, wrapper_environment, "config")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "Docker was not found" in result.stderr


def test_reports_missing_compose_v2(
    tmp_path: Path, wrapper_environment: dict[str, str]
) -> None:
    bin_dir = tmp_path / "bin"
    fake_docker(bin_dir / "docker")
    wrapper_environment.update(
        {
            "PATH": str(bin_dir),
            "COMPOSE_VERSION_STATUS": "1",
        }
    )

    result = run_wrapper(COMPOSE_SCRIPT, wrapper_environment, "config")

    assert result.returncode == 1
    assert "Docker Compose v2 is unavailable" in result.stderr
    assert trace(wrapper_environment) == ["compose version"]


def test_preserves_default_config_when_credential_helper_exists(
    tmp_path: Path, wrapper_environment: dict[str, str]
) -> None:
    bin_dir = tmp_path / "bin"
    fake_docker(bin_dir / "docker")
    write_executable(bin_dir / "docker-credential-bonski-test-helper", "#!/bin/sh\nexit 0\n")
    docker_config = Path(wrapper_environment["HOME"]) / ".docker"
    docker_config.mkdir()
    (docker_config / "config.json").write_text(
        '{"auths": {}, "credsStore": "bonski-test-helper"}'
    )
    wrapper_environment["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

    result = run_wrapper(COMPOSE_SCRIPT, wrapper_environment, "config")

    assert result.returncode == 0, result.stderr
    assert Path(wrapper_environment["CAPTURE_DOCKER_CONFIG"]).read_text() == "\n"
    assert (docker_config / "config.json").read_text() == (
        '{"auths": {}, "credsStore": "bonski-test-helper"}'
    )


def test_uses_temporary_config_when_credential_helper_is_missing(
    tmp_path: Path, wrapper_environment: dict[str, str]
) -> None:
    bin_dir = tmp_path / "bin"
    fake_docker(bin_dir / "docker")
    docker_config = Path(wrapper_environment["HOME"]) / ".docker"
    docker_config.mkdir()
    original_config = """{
  "auths": {},
  "credHelpers": {
    "https://registry.example.test": "bonski-missing-helper"
  }
}
"""
    (docker_config / "config.json").write_text(
        original_config
    )
    wrapper_environment["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

    result = run_wrapper(COMPOSE_SCRIPT, wrapper_environment, "config")

    temporary_config = Path(
        Path(wrapper_environment["CAPTURE_DOCKER_CONFIG"]).read_text().strip()
    )
    assert result.returncode == 0, result.stderr
    assert temporary_config != docker_config
    assert Path(wrapper_environment["CAPTURE_CONFIG_JSON"]).read_text() == '{"auths":{}}\n'
    assert not temporary_config.exists()
    assert (docker_config / "config.json").read_text() == original_config


def test_default_config_mode_preserves_config_despite_missing_helper(
    tmp_path: Path, wrapper_environment: dict[str, str]
) -> None:
    bin_dir = tmp_path / "bin"
    fake_docker(bin_dir / "docker")
    docker_config = Path(wrapper_environment["HOME"]) / ".docker"
    docker_config.mkdir()
    (docker_config / "config.json").write_text(
        '{"auths": {}, "credsStore": "bonski-missing-helper"}'
    )
    wrapper_environment.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "BONSKI_DOCKER_CONFIG_MODE": "default",
        }
    )

    result = run_wrapper(COMPOSE_SCRIPT, wrapper_environment, "config")

    assert result.returncode == 0, result.stderr
    assert Path(wrapper_environment["CAPTURE_DOCKER_CONFIG"]).read_text() == "\n"


def test_anonymous_config_mode_forces_temporary_config(
    tmp_path: Path, wrapper_environment: dict[str, str]
) -> None:
    bin_dir = tmp_path / "bin"
    fake_docker(bin_dir / "docker")
    wrapper_environment.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "BONSKI_DOCKER_CONFIG_MODE": "anonymous",
        }
    )

    result = run_wrapper(COMPOSE_SCRIPT, wrapper_environment, "config")

    temporary_config = Path(
        Path(wrapper_environment["CAPTURE_DOCKER_CONFIG"]).read_text().strip()
    )
    assert result.returncode == 0, result.stderr
    assert Path(wrapper_environment["CAPTURE_CONFIG_JSON"]).read_text() == '{"auths":{}}\n'
    assert not temporary_config.exists()


def test_rejects_invalid_config_mode(
    tmp_path: Path, wrapper_environment: dict[str, str]
) -> None:
    bin_dir = tmp_path / "bin"
    fake_docker(bin_dir / "docker")
    wrapper_environment.update(
        {
            "PATH": str(bin_dir),
            "BONSKI_DOCKER_CONFIG_MODE": "unsupported",
        }
    )

    result = run_wrapper(COMPOSE_SCRIPT, wrapper_environment, "config")

    assert result.returncode == 1
    assert "Invalid BONSKI_DOCKER_CONFIG_MODE" in result.stderr
    assert not Path(wrapper_environment["TRACE_FILE"]).exists()


def test_forwards_compose_exit_code(
    tmp_path: Path, wrapper_environment: dict[str, str]
) -> None:
    bin_dir = tmp_path / "bin"
    fake_docker(bin_dir / "docker")
    wrapper_environment.update(
        {
            "PATH": str(bin_dir),
            "COMPOSE_STATUS": "23",
        }
    )

    result = run_wrapper(COMPOSE_SCRIPT, wrapper_environment, "config")

    assert result.returncode == 23
    assert trace(wrapper_environment) == ["compose version", "compose config"]


def test_legacy_entry_point_works_from_another_directory(
    tmp_path: Path, wrapper_environment: dict[str, str]
) -> None:
    bin_dir = tmp_path / "bin"
    fake_docker(bin_dir / "docker")
    other_directory = tmp_path / "elsewhere"
    other_directory.mkdir()
    wrapper_environment["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

    result = run_wrapper(
        LEGACY_SCRIPT,
        wrapper_environment,
        "config",
        cwd=other_directory,
    )

    assert result.returncode == 0, result.stderr
    assert trace(wrapper_environment) == ["compose version", "compose config"]
