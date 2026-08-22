# Miso storage and backup layout

This is the filesystem contract for Miso on Pancho Pi. It keeps transactional
state on the journaled root ext4 filesystem and uses the Samsung T7 exFAT
filesystem only for backup artifacts.

## Managed paths

| Path | Filesystem | Owner/mode | Purpose |
| --- | --- | --- | --- |
| `/var/lib/miso/db` | root ext4 | `miso:miso`, 0750 | Live `miso.sqlite3`, WAL, and shared-memory files |
| `/var/lib/miso/state` | root ext4 | `miso:miso`, 0750 | Durable non-database application state |
| `/var/lib/miso/models` | root ext4 | `miso:miso`, 0750 | Explicitly managed local model files and manifests |
| `/run/miso` | tmpfs | created by systemd | Sockets, locks, and other restart-disposable runtime files |
| `/etc/miso` | root ext4 | `root:miso`, 0750 | Service configuration; secret files must be 0640 or stricter |
| `/media/pancho/T7/backups/miso` | T7 exFAT | mount-mapped `pancho:pancho`, 0755 | Completed encrypted backup artifacts only |

The service account is the system user and group `miso`, with no login shell.
The application must not place a SQLite database, WAL, Unix socket, model being
downloaded, or other live mutable state on the T7. Model downloads should use a
temporary name in `/var/lib/miso/models` and rename only after checksum
verification.

The exFAT mount maps every object to `pancho:pancho` and does not support Unix
per-file ownership or restrictive modes. A root-run backup service therefore
writes only encrypted artifacts there; plaintext staging stays on ext4.

## Space policy

- Warn when root has less than 30 GiB free; stop model downloads and other
  discretionary growth below 20 GiB free.
- Keep the installed model set below 80 GiB and require at least 40 GiB free
  after a proposed model installation.
- Warn when the T7 has less than 200 GiB free and do not start a Miso backup
  below 100 GiB free.
- Limit Miso backup artifacts to 20 GiB in addition to time-based retention, so
  they cannot unexpectedly consume the existing media allocation.

These thresholds leave room for the OS, Docker, existing service databases, and
temporary SQLite maintenance on the 234 GiB root filesystem. They are initial
guardrails and should be adjusted only from measured production growth.

## Backup and restore contract

The automation tracked by `miso-afm.4.7` must use SQLite's online backup API
(`sqlite3 .backup` or the equivalent application API), never a plain copy of a
live database. It should:

1. Create and integrity-check a database snapshot in a private temporary
   directory on root ext4.
2. Package the database, durable state, configuration manifest, and checksums.
   Models are reproducible and are excluded.
3. Encrypt private application data before it leaves ext4. The existing
   recovery passphrase may be reused, but it must remain outside Git.
4. Write a uniquely named `.partial` artifact on the T7, verify it, then rename
   it to its final name in the same T7 directory.
5. Keep 30 daily recovery points, subject to the 20 GiB hard allocation. Never
   delete the newest verified recovery point. A failed backup must not trigger
   retention deletion.

A restore is first extracted to a private ext4 staging directory and checked
with `PRAGMA integrity_check`. For a real replacement, stop Miso, place the
verified database beside the live database with `miso:miso` ownership and mode
0640, fsync it, rename it atomically over the live path, then start Miso and run
application-level checks. Keep the displaced database until that validation
passes.

`ops/verify-miso-storage-layout.sh` tests the layout with synthetic data. It
creates a WAL-mode SQLite fixture on ext4, takes an online backup, stages it on
the T7 through a `.partial` name, restores it to ext4, and checks both integrity
and content. It never reads or replaces application data.
