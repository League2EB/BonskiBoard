#!/bin/sh
# Portable Docker Compose v2 entry point for macOS (including OrbStack) and Linux.

set -eu

find_docker() {
    candidate=$1
    case "$candidate" in
        */*)
            if [ -x "$candidate" ]; then
                printf '%s\n' "$candidate"
                return 0
            fi
            ;;
        *)
            if command -v "$candidate" >/dev/null 2>&1; then
                command -v "$candidate"
                return 0
            fi
            ;;
    esac
    return 1
}

is_orbstack_cli() {
    candidate=$1
    case "$candidate" in
        "$HOME/.orbstack/bin/docker"|*/.orbstack/bin/docker)
            return 0
            ;;
        "$HOME/.local/bin/docker")
            target=$(readlink "$candidate" 2>/dev/null || true)
            case "$target" in
                "$HOME/.orbstack/bin/docker"|*/.orbstack/bin/docker)
                    return 0
                    ;;
            esac
            ;;
    esac
    return 1
}

docker_bin=""

if [ -n "${DOCKER_BIN:-}" ]; then
    if ! docker_bin=$(find_docker "$DOCKER_BIN"); then
        printf '%s\n' "DOCKER_BIN does not resolve to an executable Docker CLI: $DOCKER_BIN" >&2
        exit 1
    fi
fi
if [ -z "$docker_bin" ]; then
    docker_bin=$(find_docker docker || true)
fi
if [ -z "$docker_bin" ] && [ -x "$HOME/.local/bin/docker" ]; then
    docker_bin="$HOME/.local/bin/docker"
fi
if [ -z "$docker_bin" ] && [ -x "$HOME/.orbstack/bin/docker" ]; then
    docker_bin="$HOME/.orbstack/bin/docker"
fi

if [ -z "$docker_bin" ]; then
    printf '%s\n' "Docker was not found. Set DOCKER_BIN, install docker on PATH, or install it in ~/.local/bin or OrbStack." >&2
    exit 1
fi

# An explicitly selected OrbStack binary deserves the same socket convenience
# as the fallback, but never overrides a caller-provided DOCKER_HOST.
if is_orbstack_cli "$docker_bin" && [ -z "${DOCKER_HOST+x}" ] &&
    [ -S "$HOME/.orbstack/run/docker.sock" ]; then
    export DOCKER_HOST="unix://$HOME/.orbstack/run/docker.sock"
fi

config_mode=${BONSKI_DOCKER_CONFIG_MODE:-auto}
case "$config_mode" in
    auto|default|anonymous) ;;
    *)
        printf '%s\n' "Invalid BONSKI_DOCKER_CONFIG_MODE: $config_mode (expected auto, default, or anonymous)." >&2
        exit 1
        ;;
esac

temporary_docker_config=""
cleanup() {
    if [ -n "$temporary_docker_config" ]; then
        rm -rf "$temporary_docker_config"
    fi
}
trap cleanup EXIT HUP INT TERM

use_anonymous_config=0
config_dir=${DOCKER_CONFIG:-"$HOME/.docker"}

# Docker config is JSON, but portable Docker hosts do not necessarily have jq
# or Python. This deliberately narrow parser only accepts safe helper names:
# credsStore and string values inside credHelpers. It neither reads nor emits
# auths values; malformed or unsupported config is left untouched (fail open).
configured_helpers() {
    config_file=$1
    sed 's/[{}]/\
&\
/g; s/,/\
/g' "$config_file" |
        awk '
            function helper_value(line, value) {
                value = line
                sub(/^.*:[[:space:]]*"/, "", value)
                sub(/".*$/, "", value)
                return value
            }
            /"credsStore"[[:space:]]*:/ {
                value = helper_value($0)
                if (value ~ /^[A-Za-z0-9_.-]+$/) print value
            }
            /"credHelpers"[[:space:]]*:/ { in_helpers = 1; next }
            in_helpers && $0 ~ /^[[:space:]]*}[[:space:]]*$/ { in_helpers = 0; next }
            in_helpers && /:[[:space:]]*"/ {
                value = helper_value($0)
                if (value ~ /^[A-Za-z0-9_.-]+$/) print value
            }
        ' | sort -u
}

if [ "$config_mode" = "anonymous" ]; then
    use_anonymous_config=1
elif [ "$config_mode" = "auto" ] && [ -f "$config_dir/config.json" ]; then
    if [ -r "$config_dir/config.json" ]; then
        for helper in $(configured_helpers "$config_dir/config.json"); do
            if ! command -v "docker-credential-$helper" >/dev/null 2>&1; then
                use_anonymous_config=1
                break
            fi
        done
    else
        printf '%s\n' "Warning: Docker config is unreadable; retaining the current Docker configuration." >&2
    fi
fi

if [ "$use_anonymous_config" -eq 1 ]; then
    temporary_docker_config=$(mktemp -d "${TMPDIR:-/tmp}/bonski-docker.XXXXXX")
    printf '%s\n' '{"auths":{}}' > "$temporary_docker_config/config.json"

    plugin_dir=""
    if [ -d "$config_dir/cli-plugins" ]; then
        plugin_dir="$config_dir/cli-plugins"
    elif [ -d "$HOME/.docker/cli-plugins" ]; then
        plugin_dir="$HOME/.docker/cli-plugins"
    fi
    if [ -n "$plugin_dir" ]; then
        ln -s "$plugin_dir" "$temporary_docker_config/cli-plugins"
    fi
    export DOCKER_CONFIG="$temporary_docker_config"
fi

if ! "$docker_bin" compose version >/dev/null 2>&1; then
    printf '%s\n' "Docker Compose v2 is unavailable. Install or enable 'docker compose' and try again." >&2
    exit 1
fi

"$docker_bin" compose "$@"
