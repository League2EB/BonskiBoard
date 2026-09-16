#!/bin/sh
# Use this wrapper only on this Mac when Docker's saved credential helper is
# unavailable. It keeps Docker credentials untouched and uses OrbStack's socket.

set -eu

docker_bin="${DOCKER_BIN:-$HOME/.local/bin/docker}"
if [ ! -x "$docker_bin" ]; then
  docker_bin="$HOME/.orbstack/bin/docker"
fi

if [ ! -x "$docker_bin" ]; then
  printf '%s\n' "Docker was not found at the configured local or OrbStack path." >&2
  exit 1
fi

temporary_docker_config="$(mktemp -d "${TMPDIR:-/tmp}/bonski-docker.XXXXXX")"
cleanup() {
  rm -rf "$temporary_docker_config"
}
trap cleanup EXIT HUP INT TERM

if [ -d "$HOME/.docker/cli-plugins" ]; then
  ln -s "$HOME/.docker/cli-plugins" "$temporary_docker_config/cli-plugins"
fi

printf '%s\n' '{"auths":{}}' > "$temporary_docker_config/config.json"
export DOCKER_CONFIG="$temporary_docker_config"

if [ -S "$HOME/.orbstack/run/docker.sock" ]; then
  export DOCKER_HOST="unix://$HOME/.orbstack/run/docker.sock"
fi

"$docker_bin" compose "$@"
