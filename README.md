# NYTarr

A lightweight, self-hosted New York Times PDF archive manager. Searches Prowlarr,
submits editions to SABnzbd, and imports completed PDFs into a chronological library.
Use only sources and content you are authorised to access. Not affiliated with The New York Times.

## Features

- Responsive dashboard, edition filters, activity logs and mobile-friendly history.
- Scheduling and backfill configured in the UI; deployment settings owned by Docker/Unraid.
- Persistent SAB job IDs, restart recovery and minute-by-minute download monitoring.
- Atomic configuration/history writes and staged PDF imports across filesystems.
- Waitress server, CSRF protection, optional web authentication and container health check.

## Deployment status

The GHCR build workflow and Unraid template are included. **An image is not published
merely by checking out this branch.** After merging, `main` builds publish `edge`;
a version tag such as `v0.1.0` publishes version tags and `latest`. The first GHCR
package may need its visibility changed to Public before anonymous Unraid pulls work.
The workflow tests and builds both amd64 and arm64. Local Docker/Unraid acceptance
and real Prowlarr/SAB integration still need testing before a production rollout.

## Docker Compose

1. Copy `.env.example` to `.env`, enter service URLs, API keys, and **absolute host paths**.
2. Give the configured PUID/PGID write access to the downloads and archive directories.
3. For a source checkout: `docker compose up -d --build`.
   Once published: `docker compose pull && docker compose up -d --no-build`.
4. Open `http://SERVER:6789` (or your configured `WEB_PORT`).
5. Set your routine in Settings and click Search now.

SABnzbd must finish downloads in the host directory mapped to `/downloads`.
The archive host directory is mapped to `/media`. The containers do not need identical
internal paths, but must see the same underlying completed files. Keep archive and
download folders separate. PDFs are moved, not retained in the download folder.
File names become `New York Times - YYYY.MM.DD.pdf`.

## Unraid

Copy `unraid/nytarr.xml` into `/boot/config/plugins/dockerMan/templates-user/`
on your Unraid server, then select NYTarr under **Docker → Add Container → Template**.
This is a user template, not a Community Applications listing.

Set completed-download and archive host paths, service URLs/API keys, timezone,
UID/GID and preferably a web password. Default UID/GID are Unraid's `99:100`.
The web port's host side is configurable; the container port is normally 6789.
The template points to `latest` (requires a published release); use `edge` for
main-branch builds, or build locally until publishing is enabled.

The entrypoint starts as root only to assign ownership of `/config`, then drops to
PUID/PGID. It **never changes ownership of download or library volumes**. It also
supports Docker `user:` / `--user` with pre-provisioned writable directories.

## Configuration ownership

| Environment variable | Purpose / default |
| --- | --- |
| `PROWLARR_URL`, `PROWLARR_API_KEY` | Required for searching |
| `PROWLARR_INDEXER` | Optional exact indexer name; empty searches all |
| `SABNZBD_URL`, `SABNZBD_API_KEY` | Required for submissions/monitoring |
| `SABNZBD_CATEGORY` | `nytarr`; configure this category in SAB |
| `DOWNLOAD_FOLDER`, `MEDIA_FOLDER` | Container paths, `/downloads`, `/media` |
| `TZ` | IANA timezone, default `UTC` |
| `PUID`, `PGID` | Runtime ownership, default `99`, `100` |
| `PORT` | Internal HTTP port, default `6789` |
| `CONFIG_DIR` | Persistent appdata path, default `/config` |
| `WEB_USER`, `WEB_PASSWORD` | Optional HTTP Basic auth; default user `admin` |
| `COOKIE_SECURE` | Set `true` when accessing exclusively over HTTPS |
| `NYTARR_SECRET_KEY` | Optional stable session signing secret; otherwise random on startup |

Environment values override legacy YAML settings, including intentionally blank
values. Secrets are never rendered into the settings page. New environment secrets
are not persisted into YAML. On a UI save, legacy settings supplied by environment
are removed from YAML; other legacy settings are retained for migration.

The UI edits only daily start hour (0–23), retry interval (1–24 hours) and backfill
(0–30 previous days). Today is always included. Searches run at the start hour and
every interval thereafter, stopping at midnight in `TZ`. For example 12:00 with a
2-hour interval searches at 12, 14, 16, 18, 20 and 22. Monitoring runs every minute,
including overnight, and covers pending jobs outside the backfill window. Search now
bypasses the scheduled window. Network checks are bounded but processed serially;
large backfill runs can delay monitoring. No automatic catch-up search on startup.

First boot creates `/config/config.yml`. Without service settings the UI remains
available, but searching is disabled. Invalid configuration or corrupt history fails
startup rather than silently losing history and resubmitting downloads.

## Migration from the original app

1. Stop the old container. Back up its entire appdata directory and media library.
2. Reuse the appdata mapping, including `config.yml` and `grabberstate.json`.
3. Set container environment settings to match the old services, category and folders.
   Legacy YAML service/path values remain supported when no corresponding environment
   variable is present. The bundled Compose file supplies these variables explicitly.
4. Existing `downloading` entries with SAB IDs resume monitoring. Existing `completed`
   entries are preserved. Changing library mappings does not rewrite historical paths.
5. Check permissions and test one edition before enabling unattended operation.

## Failures and safe recovery

Submission intent is persisted **before** contacting SAB. A timeout or restart in that
window produces `review` (or briefly `submitting`) instead of an automatic duplicate.
Unknown/missing SAB jobs retain their IDs; transient API failures never cause a resubmit.
Failed jobs are also held for review, avoiding loops downloading the same failed release.

For now recovery is deliberately manual: check SAB's queue/history and the destination
first. If an uncertain submission exists in SAB, stop NYTarr, back up
`grabberstate.json`, set that date's `status` to `downloading` and `nzo_id` to the real SAB
ID, then restart. If no submission or completed file exists, stop NYTarr, back up the
state file, remove only that date's entry and restart, then run Search now. Never edit
history while the app is running. There is not yet an in-UI retry/reset control.

Imports require a matching NYT date in the PDF filename and a `%PDF-` header. SAB must
report completion first. Nested folders are supported; symlinks outside the configured
source are not imported. Files without matching names remain pending and require review.
State writes and staged imports use atomic replacement; only one NYTarr instance may
use an appdata/library pair. Do not run multiple web worker processes or replicas.

## Security and health

Use on a trusted LAN or behind a TLS reverse proxy/VPN. Basic auth over plain HTTP is
not encrypted. With no `WEB_PASSWORD`, anyone with network access can operate the UI.
Do not expose an unauthenticated instance to the internet. API keys in Docker environment
are visible to Docker administrators; Unraid's masked inputs are not secret encryption.
Existing historical log files are not retroactively sanitised; remove old sensitive logs
when migrating. New service error logs omit raw exception URLs and response bodies.

`GET /health` is unauthenticated and reports process/scheduler health, not upstream
service availability. Docker uses it for health status. SIGTERM stops scheduling and
waits for bounded in-flight work; allow a 120-second stop timeout.

## Development and tests

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
CONFIG_DIR=./local-config .venv/bin/python app.py
```

Tests cover routes, CSRF/auth, settings validation, secret handling, restart recovery,
ambiguous submissions, PDF import, schedule timezone/window and fail-closed history.
They use mocked services; no real downloads are performed.
