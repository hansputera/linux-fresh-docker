#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

COMPOSE=(docker compose -f "$ROOT/docker-compose.yml")

die() {
  echo "error: $*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"
}

generate() {
  need python3
  python3 "$ROOT/generate.py"
}

check() {
  need python3
  python3 "$ROOT/generate.py" >/dev/null
  python3 "$ROOT/generate.py" --check "$@"
}

resolve_name() {
  python3 "$ROOT/generate.py" --resolve "${1:-}"
}

ssh_port() {
  python3 "$ROOT/generate.py" --ssh-port "${1:-}"
}

up() {
  need docker
  generate
  python3 "$ROOT/generate.py" --check "$@"
  "${COMPOSE[@]}" up -d --build "$@"
  echo
  echo "Server(s) are up."
  echo "  shell:  $0 shell [service]"
  echo "  ssh:    $0 ssh [service]"
}

down() {
  need docker
  if [[ ! -f "$ROOT/docker-compose.yml" ]]; then
    generate
  fi
  if [[ $# -gt 0 ]]; then
    "${COMPOSE[@]}" stop "$@"
    "${COMPOSE[@]}" rm -f "$@"
  else
    "${COMPOSE[@]}" down
  fi
}

rebuild() {
  need docker
  generate
  python3 "$ROOT/generate.py" --check "$@"
  "${COMPOSE[@]}" build --no-cache "$@"
  "${COMPOSE[@]}" up -d "$@"
}

shell() {
  need docker
  generate >/dev/null
  local name
  name="$(resolve_name "${1:-}")"
  docker exec -it "$name" bash
}

ssh_in() {
  need ssh
  generate >/dev/null
  local name port
  name="$(resolve_name "${1:-}")"
  port="$(ssh_port "$name")"
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p "$port" root@127.0.0.1
}

logs() {
  need docker
  "${COMPOSE[@]}" logs -f --tail=100 "$@"
}

status() {
  need docker
  "${COMPOSE[@]}" ps
}

show() {
  generate
  echo
  echo "===== docker-compose.yml ====="
  cat "$ROOT/docker-compose.yml"
}

list_services() {
  need python3
  python3 "$ROOT/generate.py" --list-services
}

usage() {
  cat <<EOF
Fresh Linux server simulator

Usage: $0 <command> [service]

  up [service]        Read config.yaml, generate, build, start
  down [service]      Stop all services, or only one
  rebuild [service]   Rebuild without cache, then start
  generate            Only write Dockerfile + docker-compose.yml
  check [service]     Check port / name / network conflicts
  show                Generate and print compose file
  list                List services from config.yaml
  shell [service]     Open bash inside a server
  ssh [service]       SSH into that server's mapped port
  logs [service]      Follow logs
  status              Show compose status

Configure multiple servers in config.yaml under services:
EOF
}

cmd="${1:-}"
shift || true

case "$cmd" in
  up) up "$@" ;;
  down) down "$@" ;;
  rebuild) rebuild "$@" ;;
  generate|gen) generate ;;
  check) check "$@" ;;
  show) show ;;
  list|ls) list_services ;;
  shell|exec) shell "$@" ;;
  ssh) ssh_in "$@" ;;
  logs) logs "$@" ;;
  status|ps) status ;;
  -h|--help|help|"") usage ;;
  *)
    usage
    die "unknown command: $cmd"
    ;;
esac
