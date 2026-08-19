#!/usr/bin/env python3
"""Generate Dockerfile + docker-compose.yml from config.yaml."""

from __future__ import annotations

import argparse
import re
import socket
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"

DEFAULTS = {
    "image": "ubuntu:22.04",
    "network": "proxy",
    "network_external": True,
    "cert_resolver": "le",
    "entrypoint": "websecure",
    "backend_port": 80,
    "ssh_password": "changeme",
    "persist": True,
    "persist_type": "volume",
    "persist_dir": "./data",
    "persist_mount": "/data",
    "persist_paths": ["/root", "/opt", "/var/www"],
    "volumes": [],
    "privileged": False,
    "cap_add": [],
    "resources": {
        "cpus": "1.0",
        "memory": "1G",
        "pids": 256,
    },
}


def _strip_comment(line: str) -> str:
    in_quote = False
    quote = ""
    out = []
    for ch in line:
        if ch in ("'", '"') and not in_quote:
            in_quote = True
            quote = ch
            out.append(ch)
        elif in_quote and ch == quote:
            in_quote = False
            quote = ""
            out.append(ch)
        elif ch == "#" and not in_quote:
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _coerce(value: str):
    value = _unquote(value)
    if value == "":
        return None
    if value == "[]":
        return []
    if value == "{}":
        return {}
    low = value.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def parse_yaml(text: str):
    entries = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        if "\t" in stripped:
            raise ValueError(f"line {lineno}: use spaces, not tabs")
        indent = len(stripped) - len(stripped.lstrip(" "))
        entries.append((lineno, indent, stripped.strip()))

    def parse_value(idx: int, parent_indent: int):
        if idx >= len(entries):
            return None, idx
        lineno, indent, content = entries[idx]
        if indent <= parent_indent:
            return None, idx
        if content.startswith("- "):
            return parse_list(idx, indent)
        return parse_map(idx, indent)

    def parse_list(idx: int, list_indent: int):
        items = []
        while idx < len(entries):
            lineno, indent, content = entries[idx]
            if indent != list_indent or not content.startswith("- "):
                break
            item_text = content[2:].strip()
            idx += 1
            if item_text == "" or item_text.endswith(":") and item_text[:-1].strip() and _coerce(item_text.split(":", 1)[1] if ":" in item_text else "") is None:
                nested, idx = parse_value(idx, list_indent)
                if item_text.endswith(":") and item_text[:-1].strip():
                    items.append({item_text[:-1].strip(): nested})
                else:
                    items.append(nested)
            elif ":" in item_text and not item_text.startswith(("http://", "https://")):
                key, value = item_text.split(":", 1)
                coerced = _coerce(value)
                if coerced is None:
                    nested, idx = parse_value(idx, list_indent)
                    items.append({key.strip(): nested})
                else:
                    items.append({key.strip(): coerced})
            else:
                items.append(_coerce(item_text))
        return items, idx

    def parse_map(idx: int, map_indent: int):
        data = {}
        while idx < len(entries):
            lineno, indent, content = entries[idx]
            if indent != map_indent:
                break
            if content.startswith("- "):
                raise ValueError(f"line {lineno}: unexpected list item in mapping")
            if ":" not in content:
                raise ValueError(f"line {lineno}: expected key: value")
            key, rest = content.split(":", 1)
            key = key.strip()
            value = _coerce(rest)
            idx += 1
            if value is None:
                nested, idx = parse_value(idx, map_indent)
                data[key] = {} if nested is None else nested
            else:
                data[key] = value
        return data, idx

    if not entries:
        return {}
    first_indent = entries[0][1]
    parsed, idx = parse_map(0, first_indent)
    if idx != len(entries):
        lineno = entries[idx][0]
        raise ValueError(f"line {lineno}: unexpected indentation")
    return parsed


def load_config(path: Path) -> dict:
    return parse_yaml(path.read_text(encoding="utf-8"))


def parse_volume(spec: str) -> dict:
    raw = str(spec).strip()
    if not raw:
        raise SystemExit("empty volume mapping")
    parts = raw.split(":")
    mode = None
    if len(parts) >= 3 and parts[-1] in {"ro", "rw", "z", "Z"}:
        mode = parts[-1]
        parts = parts[:-1]
    if len(parts) == 1:
        return {"type": "anon", "source": None, "dest": parts[0], "raw": raw, "mode": mode}
    source, dest = parts[0], ":".join(parts[1:])
    if dest in {"", "/"}:
        raise SystemExit(f"invalid volume destination {raw!r}: cannot mount on '/'")
    if source.startswith(".") or source.startswith("/"):
        kind = "bind"
    else:
        kind = "named"
    return {"type": kind, "source": source, "dest": dest, "raw": raw, "mode": mode}


MEMORY_RE = re.compile(r"^\d+(\.\d+)?([kKmMgGtT]i?[bB]?)?$")


def validate_resources(svc: dict) -> None:
    res = svc.get("resources") or {}
    if not res:
        return
    name = svc["name"]
    if "cpus" in res and res["cpus"] not in (None, ""):
        try:
            cpus = float(res["cpus"])
        except (TypeError, ValueError):
            raise SystemExit(f"{name}: resources.cpus must be a number")
        if cpus <= 0:
            raise SystemExit(f"{name}: resources.cpus must be > 0")
        res["cpus"] = str(res["cpus"])
    if "cpus_reservation" in res and res["cpus_reservation"] not in (None, ""):
        try:
            if float(res["cpus_reservation"]) <= 0:
                raise SystemExit(f"{name}: resources.cpus_reservation must be > 0")
        except (TypeError, ValueError):
            raise SystemExit(f"{name}: resources.cpus_reservation must be a number")
    for key in ("memory", "memory_reservation"):
        value = res.get(key)
        if value in (None, ""):
            continue
        if not MEMORY_RE.fullmatch(str(value)):
            raise SystemExit(
                f"{name}: resources.{key} must look like 512m, 1G, 2048M"
            )
    if res.get("pids") not in (None, ""):
        try:
            pids = int(res["pids"])
        except (TypeError, ValueError):
            raise SystemExit(f"{name}: resources.pids must be an integer")
        if pids <= 0:
            raise SystemExit(f"{name}: resources.pids must be > 0")
        res["pids"] = pids


def render_resources(svc: dict) -> str:
    res = svc.get("resources") or {}
    if not res:
        return ""
    lines = []
    if res.get("cpus") not in (None, ""):
        lines.append(f'    cpus: "{res["cpus"]}"')
    if res.get("memory") not in (None, ""):
        lines.append(f'    mem_limit: {res["memory"]}')
    if res.get("memory_reservation") not in (None, ""):
        lines.append(f'    mem_reservation: {res["memory_reservation"]}')
    if res.get("pids") not in (None, ""):
        lines.append(f'    pids_limit: {res["pids"]}')

    limit_lines = []
    reserve_lines = []
    if res.get("cpus") not in (None, ""):
        limit_lines.append(f'          cpus: "{res["cpus"]}"')
    if res.get("memory") not in (None, ""):
        limit_lines.append(f'          memory: {res["memory"]}')
    if res.get("pids") not in (None, ""):
        limit_lines.append(f'          pids: {res["pids"]}')
    if res.get("cpus_reservation") not in (None, ""):
        reserve_lines.append(f'          cpus: "{res["cpus_reservation"]}"')
    if res.get("memory_reservation") not in (None, ""):
        reserve_lines.append(f'          memory: {res["memory_reservation"]}')

    if limit_lines or reserve_lines:
        lines.append("    deploy:")
        lines.append("      resources:")
        if limit_lines:
            lines.append("        limits:")
            lines.extend(limit_lines)
        if reserve_lines:
            lines.append("        reservations:")
            lines.extend(reserve_lines)
    return ("\n".join(lines) + "\n") if lines else ""


def persist_volume_name(service: str, dest: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", dest.strip("/")).strip("-").lower() or "data"
    return f"{service}-{slug}"


def build_volume_mounts(svc: dict) -> list[str]:
    mounts: list[str] = []
    if svc.get("persist", True):
        persist_type = str(svc.get("persist_type") or "volume").lower()
        if persist_type not in {"volume", "bind"}:
            raise SystemExit(
                f"{svc['name']}: persist_type must be 'volume' or 'bind', got {persist_type}"
            )
        paths = _as_list(svc.get("persist_paths"))
        persist_mount = svc.get("persist_mount") or "/data"
        if persist_mount not in paths:
            paths.append(persist_mount)
        persist_dir = str(svc.get("persist_dir") or "./data").rstrip("/")
        for dest in paths:
            dest = "/" + str(dest).lstrip("/")
            if dest in {"", "/"}:
                raise SystemExit(
                    f"{svc['name']}: cannot mount a volume on '/'. "
                    "Use specific paths like /root, /opt, /var/www, /data."
                )
            if persist_type == "bind":
                rel = dest.lstrip("/")
                src = f"{persist_dir}/{svc['name']}/{rel}"
            else:
                src = persist_volume_name(svc["name"], dest)
            mounts.append(f"{src}:{dest}")
    mounts.extend(str(item) for item in _as_list(svc.get("volumes")))
    return mounts


def named_volumes(cfg: dict) -> list[str]:
    names = []
    seen = set()
    for svc in cfg["services"]:
        for spec in svc.get("volume_mounts", []):
            parsed = parse_volume(spec)
            if parsed["type"] == "named" and parsed["source"] not in seen:
                seen.add(parsed["source"])
                names.append(parsed["source"])
    return names


def ensure_bind_dirs(cfg: dict) -> None:
    for svc in cfg["services"]:
        for spec in svc.get("volume_mounts", []):
            parsed = parse_volume(spec)
            if parsed["type"] != "bind" or not parsed["source"]:
                continue
            src = Path(parsed["source"])
            if not src.is_absolute():
                src = ROOT / src
            src.mkdir(parents=True, exist_ok=True)


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return list(value)
    raise SystemExit(f"expected a list, got {type(value).__name__}")


def _merge_service(name: str, raw: dict | None, defaults: dict) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SystemExit(f"services.{name} must be a mapping")

    raw = dict(raw)
    extra_volumes = _as_list(raw.pop("volumes", None))
    extra_paths = raw.pop("persist_paths", None)
    extra_resources = raw.pop("resources", None)
    extra_caps = raw.pop("cap_add", None)

    svc = dict(defaults)
    svc.update({k: v for k, v in raw.items() if v is not None})
    svc["name"] = name
    svc.setdefault("hostname", name)
    svc.setdefault("container_name", name)
    resources = dict(defaults.get("resources") or {})
    if extra_resources is None:
        pass
    elif not isinstance(extra_resources, dict):
        raise SystemExit(f"services.{name}: resources must be a mapping")
    else:
        resources.update({k: v for k, v in extra_resources.items() if v is not None})
    svc["resources"] = resources
    if extra_caps is None:
        svc["cap_add"] = _as_list(defaults.get("cap_add"))
    else:
        svc["cap_add"] = _as_list(extra_caps)
    svc["volumes"] = _as_list(defaults.get("volumes")) + extra_volumes
    if extra_paths is None:
        svc["persist_paths"] = _as_list(defaults.get("persist_paths"))
    else:
        svc["persist_paths"] = _as_list(extra_paths)

    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
        raise SystemExit(f"invalid service name: {name}")
    if svc.get("ssh_port") in (None, ""):
        raise SystemExit(f"services.{name}: ssh_port is required")
    domains = svc.get("domains") or []
    if isinstance(domains, str):
        domains = [domains]
    if not isinstance(domains, list) or not domains:
        raise SystemExit(f"services.{name}: add at least one domain")
    svc["domains"] = [str(d) for d in domains]
    svc["volume_mounts"] = build_volume_mounts(svc)
    validate_resources(svc)
    return svc


def normalize(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        raise SystemExit("config.yaml must be a mapping")

    # Backward compatible: old single-service schema
    if "services" not in cfg and cfg.get("name"):
        name = str(cfg.pop("name"))
        cfg = {
            "defaults": {k: v for k, v in cfg.items() if k != "domains"},
            "services": {name: {"domains": cfg.get("domains"), **{
                k: v for k, v in cfg.items() if k != "domains"
            }}},
        }

    defaults = dict(DEFAULTS)
    raw_defaults = cfg.get("defaults") or {}
    if not isinstance(raw_defaults, dict):
        raise SystemExit("defaults must be a mapping")
    defaults.update(raw_defaults)

    raw_services = cfg.get("services")
    if not raw_services or not isinstance(raw_services, dict):
        raise SystemExit("config.yaml: add at least one entry under services:")

    services = [_merge_service(name, raw, defaults) for name, raw in raw_services.items()]

    errors = []
    ports = {}
    domains = {}
    names = {}
    routers = {}
    bind_sources = {}
    for svc in services:
        try:
            port = int(svc["ssh_port"])
        except (TypeError, ValueError):
            errors.append(f"{svc['name']}: ssh_port must be an integer")
            continue
        svc["ssh_port"] = port
        if port < 1 or port > 65535:
            errors.append(f"{svc['name']}: ssh_port {port} is not a valid TCP port")
        if port in ports:
            errors.append(
                f"ssh_port conflict: {port} used by {ports[port]} and {svc['name']}"
            )
        ports[port] = svc["name"]

        cname = str(svc["container_name"])
        if cname in names:
            errors.append(
                f"container_name conflict: {cname} used by {names[cname]} and {svc['name']}"
            )
        names[cname] = svc["name"]

        for domain in svc["domains"]:
            if domain in domains:
                errors.append(
                    f"domain conflict: {domain} used by {domains[domain]} and {svc['name']}"
                )
            domains[domain] = svc["name"]
            router = router_name(svc["name"], domain)
            if router in routers:
                errors.append(
                    f"traefik router conflict: {router} from {routers[router]} and {svc['name']}"
                )
            routers[router] = f"{svc['name']} ({domain})"

        dests = {}
        for spec in svc.get("volume_mounts", []):
            parsed = parse_volume(spec)
            dest = parsed["dest"]
            if dest in dests:
                errors.append(
                    f"{svc['name']}: volume destination conflict {dest}"
                )
            dests[dest] = spec
            if parsed["type"] == "bind":
                src = str(Path(parsed["source"]).resolve()) if parsed["source"].startswith(("/", ".")) else parsed["source"]
                try:
                    src = str((ROOT / parsed["source"]).resolve()) if not parsed["source"].startswith("/") else str(Path(parsed["source"]).resolve())
                except OSError:
                    src = parsed["source"]
                if src in bind_sources and bind_sources[src] != svc["name"]:
                    errors.append(
                        f"volume bind conflict: {parsed['source']} used by {bind_sources[src]} and {svc['name']}"
                    )
                bind_sources[src] = svc["name"]

    if errors:
        raise SystemExit("config conflict:\n- " + "\n- ".join(errors))

    return {"defaults": defaults, "services": services}


def router_name(service: str, domain: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", domain).strip("-").lower()
    return f"{service}-{slug}"[:63]


def render_dockerfile() -> str:
    return """ARG BASE_IMAGE=ubuntu:22.04
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8

ARG SSH_PASSWORD=changeme

# Typical baseline on a new Ubuntu cloud/production VM — not the app stack.
RUN apt-get update \\
    && apt-get install -y --no-install-recommends \\
        ca-certificates \\
        curl \\
        wget \\
        gnupg \\
        lsb-release \\
        sudo \\
        openssh-server \\
        vim \\
        nano \\
        less \\
        iproute2 \\
        iputils-ping \\
        procps \\
    && mkdir -p /var/run/sshd \\
    && echo "root:${SSH_PASSWORD}" | chpasswd \\
    && sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \\
    && sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config \\
    && apt-get clean \\
    && rm -rf /var/lib/apt/lists/*

EXPOSE 22 80 443

# Keep the "server" up like a VM. Install your software later on :80.
CMD ["/usr/sbin/sshd", "-D"]
"""


def _service_block(svc: dict) -> str:
    labels = [
        '      - "traefik.enable=true"',
        f'      - "traefik.docker.network={svc["network"]}"',
    ]
    for domain in svc["domains"]:
        router = router_name(svc["name"], domain)
        labels.append(f'      - "traefik.http.routers.{router}.rule=Host(`{domain}`)"')
        labels.append(f'      - "traefik.http.routers.{router}.entrypoints={svc["entrypoint"]}"')
        labels.append(f'      - "traefik.http.routers.{router}.tls.certresolver={svc["cert_resolver"]}"')
        labels.append(f'      - "traefik.http.routers.{router}.service={svc["name"]}"')
    labels.append(
        f'      - "traefik.http.services.{svc["name"]}.loadbalancer.server.port={svc["backend_port"]}"'
    )
    labels_block = "\n".join(labels)
    volume_lines = "\n".join(f'      - "{item}"' for item in svc.get("volume_mounts", []))
    volumes_block = f"    volumes:\n{volume_lines}\n" if volume_lines else ""
    resources_block = render_resources(svc)
    extra_priv = ""
    if svc.get("privileged") in (True, "true", "yes"):
        extra_priv += "    privileged: true\n"
    caps = [str(c) for c in _as_list(svc.get("cap_add")) if str(c)]
    if caps:
        extra_priv += "    cap_add:\n"
        extra_priv += "".join(f"      - {c}\n" for c in caps)
    return f"""  {svc['name']}:
    build:
      context: .
      args:
        BASE_IMAGE: {svc['image']}
        SSH_PASSWORD: {svc['ssh_password']}
    image: linux-fresh:{svc['name']}
    container_name: {svc['container_name']}
    hostname: {svc['hostname']}
    restart: unless-stopped
{extra_priv}    stdin_open: true
    tty: true
    networks:
      - {svc['network']}
    ports:
      - "{svc['ssh_port']}:22"
{resources_block}{volumes_block}    labels:
{labels_block}
"""


def render_compose(cfg: dict) -> str:
    networks = {}
    for svc in cfg["services"]:
        name = svc["network"]
        external = bool(svc["network_external"])
        if name in networks and networks[name] != external:
            raise SystemExit(f"network {name} has mixed network_external values")
        networks[name] = external

    blocks = [_service_block(svc) for svc in cfg["services"]]
    net_lines = []
    for name, external in networks.items():
        net_lines.append(f"  {name}:")
        net_lines.append(f"    external: {'true' if external else 'false'}")

    named = named_volumes(cfg)
    named_block = ""
    if named:
        named_block = "\nvolumes:\n" + "\n".join(f"  {name}:" for name in named) + "\n"

    return (
        "# Generated from config.yaml — do not edit by hand.\n"
        "# Regenerate with: ./manage.sh generate\n\n"
        "services:\n"
        + "\n".join(blocks)
        + "\nnetworks:\n"
        + "\n".join(net_lines)
        + "\n"
        + named_block
    )


def _run_docker(args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["docker", *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def docker_networks() -> set[str]:
    lines = _run_docker(["network", "ls", "--format", "{{.Name}}"])
    return {line.strip() for line in lines.splitlines() if line.strip()}


def docker_containers() -> list[dict]:
    raw = _run_docker(["ps", "-a", "--format", "{{.Names}}\t{{.Ports}}\t{{.Status}}"])
    items = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        ports = parts[1].strip()
        status = parts[2].strip() if len(parts) > 2 else ""
        published = set()
        for match in re.finditer(r"(?:0\.0\.0\.0|::|\[::\]):(\d+)->", ports):
            published.add(int(match.group(1)))
        items.append({"name": name, "ports": published, "status": status, "raw_ports": ports})
    return items


def host_port_in_use(port: int) -> bool:
    for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            sock.bind((address, port))
        except OSError:
            return True
        finally:
            sock.close()
    return False


def collect_conflicts(cfg: dict, only: list[str] | None = None) -> tuple[list[str], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    ok: list[str] = []
    wanted = set(only or [])
    services = [
        svc for svc in cfg["services"]
        if not wanted or svc["name"] in wanted
    ]

    containers = docker_containers()
    by_name = {c["name"]: c for c in containers}
    port_owners: dict[int, list[str]] = {}
    for c in containers:
        for port in c["ports"]:
            port_owners.setdefault(port, []).append(c["name"])

    networks = docker_networks()

    for svc in services:
        port = int(svc["ssh_port"])
        name = svc["container_name"]

        if port < 1024:
            warnings.append(f"{svc['name']}: ssh_port {port} is privileged; root is required on the host")

        owners = [n for n in port_owners.get(port, []) if n != name]
        if owners:
            errors.append(
                f"{svc['name']}: host port {port} already published by container {', '.join(owners)}"
            )
        elif host_port_in_use(port) and name not in by_name:
            errors.append(
                f"{svc['name']}: host port {port} is already in use on this machine"
            )
        else:
            ok.append(f"{svc['name']}: ssh port {port} is free (or already owned by this service)")

        existing = by_name.get(name)
        if existing:
            ok.append(f"{svc['name']}: container {name} already exists and will be reused")
        else:
            ok.append(f"{svc['name']}: container name {name} is available")

        if svc["network_external"]:
            if networks and svc["network"] not in networks:
                errors.append(
                    f"{svc['name']}: docker network {svc['network']!r} does not exist"
                )
            elif svc["network"] in networks:
                ok.append(f"{svc['name']}: network {svc['network']} exists")
            else:
                warnings.append(
                    f"{svc['name']}: could not list docker networks; skip existence check for {svc['network']}"
                )

    return errors, warnings, ok


def print_check(cfg: dict, only: list[str] | None = None) -> int:
    errors, warnings, ok = collect_conflicts(cfg, only=only)
    print("Conflict check")
    for line in ok:
        print(f"  OK   {line}")
    for line in warnings:
        print(f"  WARN {line}")
    for line in errors:
        print(f"  FAIL {line}")
    if errors:
        print("\nFix the conflicts in config.yaml or free the ports, then retry.")
        return 1
    print("\nNo conflicts found.")
    return 0


def resolve_service(cfg: dict, name: str | None) -> dict:
    services = cfg["services"]
    if name:
        for svc in services:
            if svc["name"] == name:
                return svc
        known = ", ".join(svc["name"] for svc in services)
        raise SystemExit(f"unknown service {name!r}. known: {known}")
    if len(services) == 1:
        return services[0]
    known = ", ".join(svc["name"] for svc in services)
    raise SystemExit(f"multiple services, specify one: {known}")


def print_summary(cfg: dict) -> None:
    print(f"Generated Dockerfile and docker-compose.yml from {CONFIG_PATH.name}")
    for svc in cfg["services"]:
        print(f"- {svc['name']}")
        print(f"    image:    {svc['image']}")
        print(f"    ssh:      localhost:{svc['ssh_port']}")
        print(f"    network:  {svc['network']}")
        print("    domains:")
        for domain in svc["domains"]:
            print(f"      - {domain}")
        if svc.get("volume_mounts"):
            print("    volumes:")
            for mount in svc["volume_mounts"]:
                print(f"      - {mount}")
        res = svc.get("resources") or {}
        if res:
            print("    resources:")
            for key in ("cpus", "memory", "pids", "memory_reservation", "cpus_reservation"):
                if res.get(key) not in (None, ""):
                    print(f"      {key}: {res[key]}")


def generate_files() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"Missing {CONFIG_PATH}")
    cfg = normalize(load_config(CONFIG_PATH))
    (ROOT / "Dockerfile").write_text(render_dockerfile(), encoding="utf-8")
    (ROOT / "docker-compose.yml").write_text(render_compose(cfg), encoding="utf-8")
    ensure_bind_dirs(cfg)
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--list-services", action="store_true")
    parser.add_argument("--ssh-port")
    parser.add_argument("--resolve")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("services", nargs="*")
    args = parser.parse_args()

    cfg = normalize(load_config(CONFIG_PATH)) if CONFIG_PATH.exists() else None

    if args.list_services:
        if not cfg:
            raise SystemExit(f"Missing {CONFIG_PATH}")
        for svc in cfg["services"]:
            print(svc["name"])
        return 0

    if args.ssh_port:
        if not cfg:
            raise SystemExit(f"Missing {CONFIG_PATH}")
        print(resolve_service(cfg, args.ssh_port)["ssh_port"])
        return 0

    if args.resolve:
        if not cfg:
            raise SystemExit(f"Missing {CONFIG_PATH}")
        print(resolve_service(cfg, args.resolve)["name"])
        return 0

    if args.check:
        if not cfg:
            raise SystemExit(f"Missing {CONFIG_PATH}")
        return print_check(cfg, only=args.services or None)

    cfg = generate_files()
    print_summary(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
