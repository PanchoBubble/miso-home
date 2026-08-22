# Pancho Pi service and recovery inventory

Captured read-only on 2026-08-22 for Beads issue `miso-afm.1.1`. No host,
service, container, network, or storage configuration was changed during the
inventory.

## Host baseline

| Item | Observed state |
| --- | --- |
| SSH | Local alias `pancho-pi`, host `rsp.local`, user `pancho` |
| OS | Debian 12 (bookworm), aarch64 |
| Kernel | `6.12.34+rpt-rpi-2712` |
| Docker | Engine 28.3.1; Compose projects `services` and `dmaga` |
| Memory | 15 GiB total, 2.8 GiB used, 13 GiB available; 512 MiB swap unused |
| Thermal | 58.7 C; `get_throttled=0x0` |
| Root disk | 234 GiB ext4, 66 GiB used, 157 GiB available |
| T7 | `/dev/sda1`, exFAT, UUID `081E-DA7A`, mounted at `/media/pancho/T7`; 1.9 TiB total, 787 GiB used, 1.1 TiB available |

The T7 is not declared in `/etc/fstab`. It is mounted with udisks-style options
and ownership mapped to UID/GID 1000. Containers and `zurg-watcher.service`
refer directly to `/media/pancho/T7`, so Docker can start before the real disk
is available. Beads issue `miso-40i` tracks making this dependency boot-safe.

## Network and host services

- `eth0` is up at `192.168.0.100/24`; `wlan0` is down.
- `tun0` is up at `10.100.0.2/20`. NordVPN installs the two half-default routes
  through `tun0`; the LAN default route remains via `192.168.0.1` on `eth0`.
- SSH, Docker, containerd, NetworkManager, NordVPN, cloudflared, Avahi, and
  `zurg-watcher` are running under systemd.
- Docker bridges are `services_default` (`172.18.0.0/16`) and
  `dmaga_default` (`172.30.0.0/24`).

## Compose ownership and recovery sources

| Project | Compose file | File owner | Observed state |
| --- | --- | --- | --- |
| `services` | `/var/www/services/docker-compose.yml` | `root:root`, mode 0644 | 10 running containers, two exited historical/current containers |
| `dmaga` | `/var/www/services/dmaga/docker-compose.yml` | `pancho:pancho`, mode 0644 | 9 running containers |

`/var/www/services` contains a Git directory but has no commits or remote. Its
Compose file, environment files, service configuration, and backup copies are
all untracked. Treat this directory as the only current deployment source and
protect secrets when it is backed up; do not commit `.env` files to a normal
source repository.

All inspected containers use restart policy `unless-stopped`.

## Existing services project

| Workload | Containers | Published host ports | Persistent state |
| --- | --- | --- | --- |
| Immich | `immich-server`, `immich-machine-learning`, `immich-postgres`, `immich-redis` | 2283 | Uploads: `/media/pancho/T7/services/immich/upload`; external photos: `/media/pancho/T7/media/photos` (344 GiB observed); ML cache: `/media/pancho/T7/services/immich/model-cache`; PostgreSQL: volume `services_immich-pgdata` (2.319 GiB); Redis: anonymous volume `0a2bac...` (1.803 MiB) |
| Nextcloud | `nextcloud`, `nextcloud-db`, `nextcloud-redis` | 8081 | External data: `/media/pancho/T7/services/nextcloud/data`; application/config: volume `services_nextcloud_html` (821.6 MiB); MySQL: `/var/lib/mysql-nextcloud`; Redis: anonymous volume `8fc753...` |
| Vaultwarden | `vaultwarden` | 8082 | `/var/www/services/bitwarden/bwdata` |
| Homepage | `homepage`, `homepage-external` | 3001, 3002 | T7 paths under `/media/pancho/T7/services/homepage*` |
| n8n | `n8n` | none | Exited with code 127 nine months ago; data path `/media/pancho/T7/services/n8n/data` |
| Legacy Flaresolverr | `flaresolverr` | none | Exited with code 143 nine months ago and absent from the current Compose service list |

Immich, Nextcloud, Vaultwarden, and both Homepage containers were running when
inventoried. Health checks report Immich, Vaultwarden, and Homepage healthy;
Nextcloud and its MySQL/Redis dependencies do not expose Docker health checks.

## DMAGA project

The running containers are `dmaga-app`, `dmaga-byparr`,
`dmaga-debrid-poller`, `dmaga-proxy`, `dmaga-flaresolverr`, `dmaga-redis`,
`dmaga-gluetun`, `dmaga-postgres`, and `dmaga-qbittorrent`.

Published ports are 80 for the app, 8191 for the VPN-routed helper, 8080 for
qBittorrent, and TCP/UDP 6881 for BitTorrent. PostgreSQL, Redis, qBittorrent
configuration, Node modules, and application build output are Docker volumes
on the root filesystem. Volume `dmaga_media-downloads` is a local bind volume
whose device is `/media/pancho/T7/dmaga/downloads`; its apparent Docker volume
mount is therefore T7 data, not duplicate root-disk data.

## Backup and restore status

No scheduled application or database backup timer was found. The root crontab
is empty. The `pancho` crontab runs
`/home/pancho/sync_takeout_to_juli_unlimited.sh` daily at noon, but that script
does not exist, so the image-import/sync job cannot run as configured.

Only ad-hoc configuration backup filenames were found under the deployment
tree; no usable backup set for Immich PostgreSQL/uploads, Nextcloud
MySQL/html/data, Vaultwarden data, or Compose configuration was identified.
Consequently, the services can be restarted from live state, but a disaster
restore is not currently proven. Beads issue `miso-3vg` tracks consistent
backups and restore testing.

## Safe recovery commands

First verify that the expected physical T7 is mounted. Do not start dependent
services if this check fails:

```bash
findmnt -S UUID=081E-DA7A -T /media/pancho/T7
```

Inspect and start the core services project:

```bash
docker compose -f /var/www/services/docker-compose.yml ps -a
docker compose -f /var/www/services/docker-compose.yml up -d
```

Inspect, stop, or start DMAGA independently:

```bash
docker compose -f /var/www/services/dmaga/docker-compose.yml ps -a
docker compose -f /var/www/services/dmaga/docker-compose.yml stop
docker compose -f /var/www/services/dmaga/docker-compose.yml up -d
```

Confirm service state without modifying it:

```bash
docker compose ls --all
docker ps -a
systemctl --no-pager --type=service --state=running
vcgencmd measure_temp
vcgencmd get_throttled
```

Do not run `docker compose down -v`, `docker volume prune`, or
`docker system prune --volumes`: those commands can remove unreconstructed
service data.

## Risks discovered

| Severity | Risk |
| --- | --- |
| Critical | No verified backup/restore path exists for the current databases, Vaultwarden state, and deployment source. |
| High | T7 is not a persistent boot mount even though multiple restart-enabled containers and a system service depend on its path. |
| High | Live PostgreSQL/MySQL/Vaultwarden state on the microSD has no observed consistent backup. |
| High | The only scheduled image sync references a missing script. |
| Medium | Deployment source is uncommitted, has no remote, and includes environment files and ad-hoc backup copies. |
| Medium | Several images use mutable `latest` tags, weakening reproducibility. |
| Medium | Ports 80, 2283, 3001, 3002, 8080, 8081, 8082, 8191, and 6881 bind on all host interfaces; tunnel/VPN/firewall policy should be verified before changing networking. |
| Low | Docker retains about 26 GiB of reclaimable images, but cleanup must wait until recovery sources are protected. |
