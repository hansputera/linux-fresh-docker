# Linux Fresh Server Simulator

Spin up one or more **fresh Ubuntu servers** in Docker, expose them through an existing Traefik proxy, and treat them like new production VMs (SSH, `apt`, install your own software).

Edit only `config.yaml`. Do not edit the generated Docker files by hand.

## Requirements

- Docker with Compose v2
- Python 3
- An existing Traefik instance
- An existing Docker network (default: `proxy`)
- Traefik certificate resolver named `le`

## Files

| File | Role |
| --- | --- |
| `config.yaml` | The only file you configure |
| `manage.sh` | Commands: up, down, ssh, check, … |
| `generate.py` | Builds Docker files from the config |
| `Dockerfile` | Generated |
| `docker-compose.yml` | Generated |

```bash
chmod +x manage.sh
```

## Quick start

1. Edit `config.yaml` (name, domains, SSH port, password, resources).
2. Make sure the Traefik network exists:
   ```bash
   docker network ls | grep proxy
   ```
3. Start:
   ```bash
   ./manage.sh up
   ```
4. Enter the server:
   ```bash
   ./manage.sh shell pay-dev
   # or
   ./manage.sh ssh pay-dev
   ```
5. Install your software inside the container and bind it to port `80`.

Until something listens on `:80`, Traefik will return **502**. That is expected on a fresh server.

## Commands

```bash
./manage.sh up                 # generate, check conflicts, build, start all
./manage.sh up pay-dev         # start one service
./manage.sh down
./manage.sh down pay-dev
./manage.sh rebuild pay-dev
./manage.sh generate           # only write Docker files
./manage.sh check              # port / name / domain / network conflicts
./manage.sh show
./manage.sh list
./manage.sh shell [service]
./manage.sh ssh [service]
./manage.sh logs [service]
./manage.sh status
```

If there is only one service, `shell` and `ssh` do not need a name.

Default SSH login:

- user: `root`
- host: `127.0.0.1`
- port: from `ssh_port` (example: `2221`)
- password: from `ssh_password`

## `config.yaml`

Shared settings go under `defaults`. Each server is a key under `services`.

```yaml
defaults:
  image: ubuntu:22.04
  network: proxy
  network_external: true
  cert_resolver: le
  entrypoint: websecure
  backend_port: 80
  ssh_password: changeme
  persist: true
  persist_type: volume
  persist_mount: /data
  persist_paths:
    - /root
    - /opt
    - /var/www
  resources:
    cpus: 1.0
    memory: 1G
    pids: 256

services:
  pay-dev:
    hostname: pay-dev
    ssh_port: 2221
    domains:
      - apipay-dev.example.com
      - pay-dev.example.com

  api-dev:
    ssh_port: 2222
    resources:
      cpus: 2.0
      memory: 2G
    domains:
      - api-dev.example.com
```

A service can override any default (`image`, `network`, `ssh_password`, volumes, resources, …).

### Required per service

- `ssh_port` — host port mapped to container SSH (`22`)
- `domains` — one or more hostnames for Traefik

`ssh_port` and `domains` must be unique across services.

### Traefik

Each domain becomes an HTTPS router:

- rule: `Host(domain)`
- entrypoint: `websecure` (configurable)
- cert resolver: `le` (configurable)
- backend port: `80` (configurable)

The container must join Traefik’s Docker network (`proxy` by default).

## Volumes

By default, persistence uses **Docker named volumes** (not a folder in this repo).

With service `pay-dev` and the default `persist_paths`, these volumes are created:

| Volume | Mount |
| --- | --- |
| `pay-dev-root` | `/root` |
| `pay-dev-opt` | `/opt` |
| `pay-dev-var-www` | `/var/www` |
| `pay-dev-data` | `/data` |

`persist_mount` (`/data`) is always added.

Do **not** persist `/`. Docker cannot mount a volume on the container root.

```yaml
persist_paths:
  - /root
  - /opt
  - /var/www
  # - /   # invalid
```

Extra mounts:

```yaml
services:
  pay-dev:
    volumes:
      - shared-certs:/etc/ssl/custom
      - /var/lib/fresh-servers/pay-dev/logs:/var/log
```

Named extra volumes are declared automatically in `docker-compose.yml`.

To use host folders instead of Docker volumes:

```yaml
persist_type: bind
persist_dir: /var/lib/fresh-servers
```

That would bind `/var/lib/fresh-servers/pay-dev/root` → `/root`, and so on.

`apt` packages in `/usr` are **not** in these volumes. They live in the container layer. Use `./manage.sh stop`/`start` style workflow if you need the writable layer to stay. `./manage.sh down` removes the container but keeps named volumes.

Useful volume commands:

```bash
docker volume ls
docker volume inspect pay-dev-data
```

## Resource limits

```yaml
resources:
  cpus: 1.0
  memory: 1G
  pids: 256
  memory_reservation: 256M
  cpus_reservation: 0.25
```

`memory` examples: `512m`, `1G`, `2G`.

Check live usage:

```bash
docker stats pay-dev
```

## Conflict checks

`./manage.sh up` and `./manage.sh check` verify:

- duplicate SSH ports in config
- duplicate domains
- duplicate container names
- Traefik router name clashes
- host port already in use
- port already published by another container
- missing external Docker network
- invalid volume destination (`/`)
- bind-mount path clashes

## Typical install flow

```bash
./manage.sh up
./manage.sh shell pay-dev
```

Inside the container:

```bash
apt-get update
# install nginx, caddy, or your app
# listen on 0.0.0.0:80
```

Then open:

- `https://apipay-dev.example.com.com`
- `https://pay-dev.example.com`

## Notes and limits

- This is a container, not a hypervisor VM.
- `apt` and binary installs work.
- `systemctl` usually does **not** work unless you switch to a privileged systemd image.
- Change `ssh_password` before using this on a reachable host.
- Generated `Dockerfile` and `docker-compose.yml` are overwritten by `./manage.sh generate` / `up`.
