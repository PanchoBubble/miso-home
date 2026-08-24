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

`ops/miso_backup.py` uses Python's SQLite online backup API, never a plain copy
of a live database. It:

1. Creates and integrity-checks a database snapshot in a private temporary
   directory on root ext4.
2. Packages the database, durable state, encrypted configuration, manifest, and
   per-file checksums. Models are reproducible and are excluded.
3. Encrypts private application data before it leaves ext4. The existing
   recovery passphrase may be reused, but it must remain outside Git.
4. Writes a uniquely named `.partial` artifact on the T7, verifies it, then renames
   it to its final name in the same T7 directory.
5. Keeps 30 daily recovery points, subject to the 20 GiB hard allocation. It
   never deletes the newest verified recovery point. A candidate that fails
   encryption, checksum, SQLite, schema, or isolated-restore validation remains
   a hidden partial and cannot trigger retention deletion.

Archives use AES-256-CBC with PBKDF2-SHA256 (600,000 iterations), an outer
SHA-256 sidecar, and encrypted internal checksums. The default recovery key is
`/home/pancho/.config/miso-backup/backup.key`; it must remain mode 0600 and must
also exist off the Pi. The archive is named
`miso-<UTC timestamp>.tar.gz.enc`. Plaintext exists only in a mode-0700
temporary directory beneath `/var/lib/miso` and is removed on every exit path.

`miso-database-backup.timer` runs daily at 04:15 with a randomized delay. Every
backup performs an isolated full restore before its atomic publication, then a
separate quick check of the published archive. The service logs warnings below
30 GiB free on root or 200 GiB on the T7 and refuses a new backup below 20 GiB
or 100 GiB respectively. `miso-database-restore-check.timer` independently
decrypts the latest verified point every Monday at 05:30, checks all member
hashes, runs SQLite integrity and foreign-key checks, restores to a second
SQLite file, and confirms the Miso application tables and schema version.

Install or refresh the automation after the storage layout and key exist:

```bash
sudo ops/install-miso-backup.sh
sudo systemctl start miso-database-backup.service
sudo systemctl start miso-database-restore-check.service
systemctl list-timers 'miso-database-*'
```

Optional overrides belong in root-owned `/etc/miso/miso-backup.env`. Supported
settings include `MISO_BACKUP_KEY_FILE`, `MISO_BACKUP_RETENTION_POINTS`,
`MISO_BACKUP_ALLOCATION_MAX_GIB`, and the documented path/space variables in
`Settings.from_environment`; production must not set the test-only
`MISO_BACKUP_SKIP_PLATFORM_CHECKS` or `MISO_BACKUP_ALLOW_NON_ROOT` switches.

The first production run passed on Pancho Pi on 2026-08-24. It created and
outer-checksummed a 21,984-byte encrypted recovery point, left no partial
artifacts, passed both the post-publication quick check and independent full
isolated restore, and left the live schema-v3 database and all ten existing
service containers healthy. At that run, root had 152 GiB and the T7 had
1,075 GiB available.

A real replacement is deliberately not automated by the timer. First run
`sudo /usr/local/sbin/miso-backup verify --full [archive]`. Stop Miso, decrypt
and extract that exact verified archive to a private ext4 staging directory,
and repeat `PRAGMA integrity_check` there. Place the database beside the live
database with `miso:miso` ownership and mode 0640, fsync it, rename it atomically
over the live path, then start Miso and run application-level checks. Keep the
displaced database until that validation passes. Restore the encrypted
configuration and state selectively; do not overwrite newer credentials
without reviewing them.

`ops/verify-miso-storage-layout.sh` tests the layout with synthetic data. It
creates a WAL-mode SQLite fixture on ext4, takes an online backup, stages it on
the T7 through a `.partial` name, restores it to ext4, and checks both integrity
and content. It never reads or replaces application data.
