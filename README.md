# grafana-datacore

A Docker-Compose stack that pulls performance counters from the
**DataCore SANsymphony REST API** and visualises them in Grafana via
InfluxDB. It is a modernised fork of
[`lblanc/grafana-integration`](https://github.com/lblanc/grafana-integration):

- Three lean services (Grafana, InfluxDB, Python collector) instead of
  one monolithic container with `supervisord`.
- Up-to-date base images: **Grafana OSS 13**, **InfluxDB 1.12-alpine**,
  **Python 3.12-slim**.
- Type-hinted Python 3 collector with a single dependency (`requests`).
- Auto-detection of DataCore's REST URL layout (versionless vs. `/1.0/`).
- A web setup UI with **Test connection**, hot **reload**, **status**
  panel and a live **logs** viewer.

---

## Table of contents

1. [Architecture](#architecture)
2. [What gets collected](#what-gets-collected)
3. [Deployment](#deployment)
4. [Configuration reference](#configuration-reference)
5. [Operating the stack](#operating-the-stack)
6. [Setup UI](#setup-ui)
7. [Troubleshooting](#troubleshooting)
8. [Migration notes from the original project](#migration-notes-from-the-original-project)

---

## Architecture

```
┌─────────────┐  REST   ┌──────────────┐  HTTP line proto   ┌──────────┐
│ DataCore    │◀────────│  collector   │───────────────────▶│ InfluxDB │
│ SANsymphony │         │  (Python)    │                    │   1.12   │
└─────────────┘         └──────┬───────┘                    └────┬─────┘
                               │ status.json                     │
                               ▼                                 ▼
                        ┌──────────────┐                  ┌──────────────┐
                        │  setup UI    │                  │  Grafana     │
                        │  (FastAPI)   │  /var/run/docker │   13         │
                        │              │──────────────────▶  reload     │
                        └──────────────┘                  └──────────────┘
```

Four containers:

- **`influxdb`** — time-series store (InfluxDB 1.12). The `INFLUXDB_DB`
  variable creates the database at first boot, and a non-admin user is
  provisioned for the collector and Grafana. The container runs as
  UID/GID 1500 since 1.12, but Docker named volumes handle the
  permissions transparently.
- **`grafana`** — Grafana OSS 13 with the InfluxDB datasource and a
  starter dashboard provisioned automatically.
- **`collector`** — the Python service that polls DataCore on a timer
  and writes line-protocol points to InfluxDB. Writes its current state
  to a JSON file on a shared volume.
- **`setup`** — a FastAPI web UI to edit the configuration, run
  connectivity tests, hot-reload the collector via `SIGHUP`, view the
  collector status and stream its logs.

## What gets collected

For every enabled resource category, the collector calls
`/RestService/rest.svc/<category>` to enumerate the objects, then
`/RestService/rest.svc/performance?id=<Id>` for each one. Numeric
counters become InfluxDB **fields**, and a few descriptive properties
become **tags**.

| Section name           | DataCore endpoint        | Notes |
|------------------------|--------------------------|---|
| `servers`              | `/servers`               | DataCore servers metrics |
| `servergroups`         | `/servergroups`          | Group-level metrics |
| `pools`                | `/pools`                 | Disk pool I/O and capacity |
| `poolmembers`          | `/poolmembers`           | Per-disk-in-pool metrics |
| `poollogicaldisks`     | `/poollogicaldisks?pool=…` | Listed per pool — handled automatically |
| `physicaldisks`        | `/physicaldisks`         | All physical disks |
| `sharedpools`          | `/sharedpools`           | Recommended over `pools` if shared SAN |
| `sharedphysicaldisks`  | `/sharedphysicaldisks`   | Same idea for disks |
| `virtualdisks`         | `/virtualdisks`          | Virtual disks served to hosts |
| `virtualdiskgroups`    | `/virtualdiskgroups`     | Virtual disk groups |
| `virtuallogicalunits`  | `/virtuallogicalunits`   | Path-level metrics, off by default |
| `logicaldisks`         | `/logicaldisks`          | Pass-through sources, off by default |
| `hosts`                | `/hosts`                 | Host-side initiator metrics |
| `hostgroups`           | `/hostgroups`            | Host group aggregates |
| `scsiports`            | `/scsiports`             | Server and host SCSI ports |
| `targetdevices`        | `/targetdevices`         | Off by default |
| `targetdomains`        | `/targetdomains`         | Off by default |
| `snapshots`            | `/snapshots`             | Off by default |
| `snapshotgroups`       | `/snapshotgroups`        | Off by default |
| `rollbackgroups`       | `/rollbackgroups`        | Off by default |

InfluxDB measurement layout:

- One measurement per category: `datacore_<category>` (e.g.
  `datacore_virtualdisks`, `datacore_pools`).
- Tags: `category`, `resource_id`, `resource_name`, plus any of
  `caption`, `alias`, `hostname`, `servername`, `groupname`, `poolname`,
  `hostid`, `serverid` when present.
- Fields: every numeric counter returned by `/performance`
  (e.g. `TotalBytesRead`, `TotalOperations`, `PercentAllocated`).
- Timestamp: the `CollectionTime` returned by DataCore (millisecond
  precision).

---

## Deployment

### Prerequisites

- Linux host with **Docker Engine 24+** and **docker compose v2**.
- Network reachability from the host to the DataCore REST Support
  server on port 80 (HTTP) or 443 (HTTPS).
- Outbound HTTPS to fetch base images on first build.
- A DataCore user that has read access to the REST API. Any standard
  Windows user that the SANsymphony group recognises will do.

### 1. Clone the repository

```bash
git clone <your-fork-url> grafana-datacore
cd grafana-datacore
```

### 2. Create the `.env`

```bash
cp .env.example .env
$EDITOR .env
```

Minimum to fill in:

| Variable                   | Meaning |
|----------------------------|---|
| `DCSREST`                  | IP or FQDN of the IIS host running DataCore REST Support |
| `DCSSVR`                   | Server name in the SANsymphony group (`ServerHost` header) |
| `DCSUNAME`, `DCSPWORD`     | DataCore credentials |
| `INFLUX_DB`                | Database name (created on first boot) |
| `INFLUX_USER`, `INFLUX_PASSWORD` | Non-admin user used by the collector and Grafana |
| `INFLUX_ADMIN_USER`, `INFLUX_ADMIN_PASSWORD` | Admin user, used only at first boot |
| `GF_ADMIN_USER`, `GF_ADMIN_PASSWORD` | Grafana admin login |
| `SETUP_ADMIN_USER`, `SETUP_ADMIN_PASSWORD` | Setup UI login |
| `SETUP_SECRET_KEY`         | Optional: pin the setup cookie key across restarts |

You can keep most defaults if you are deploying for evaluation. **Change
every `changeme` before exposing the stack to anything beyond your
laptop.**

### 3. Adjust `collector/collector.ini` if needed

The defaults enable the most useful categories. The most important
field is the **transport choice**:

```ini
[datacore]
scheme       = https      ; or 'http'
verify_tls   = false      ; set to true if you have a trusted certificate
api_version  =            ; leave empty to auto-detect; set '1.0' for REST 2.0/2.01
```

You can come back later and edit categories from the web setup UI
without touching the file by hand.

### 4. Build and start

```bash
docker compose build
docker compose up -d
docker compose ps
```

You should see four healthy containers. Initial pulls and image builds
take 1–3 minutes depending on your bandwidth.

### 5. First-run validation

```bash
# Tail the collector — within ~30s you should see lines like
#   "Wrote N points to InfluxDB database 'datacore'"
docker compose logs -f collector

# Or use the setup UI status panel + log viewer (see below)
```

URLs after a successful start (replace `localhost` with the host's
address as needed):

| Service       | URL                       | Default credentials |
|---------------|---------------------------|---|
| Grafana       | http://localhost:3000     | from `GF_ADMIN_USER` / `GF_ADMIN_PASSWORD` |
| Setup UI      | http://localhost:8088     | from `SETUP_ADMIN_USER` / `SETUP_ADMIN_PASSWORD` |
| InfluxDB API  | http://localhost:8086     | from `INFLUX_*` |

### 6. Open the starter dashboard

In Grafana, navigate to *Dashboards → DataCore → DataCore – Overview*.
The pre-provisioned datasource is `DataCore-InfluxDB` and the dashboard
queries the `datacore_virtualdisks` and `datacore_pools` measurements.

---

## Configuration reference

### `.env`

Used by `docker-compose.yml` to set environment variables on the
containers. The setup UI also writes this file when you save settings,
preserving keys it does not know about. See [`.env.example`](.env.example).

### `collector/collector.ini`

The single source of truth for the collector. Any change requires
either a `docker compose restart collector` or a hot **reload** from
the setup UI (sends `SIGHUP`; the new config is picked up after the
current cycle finishes). The file is structured into sections.

#### `[datacore]`

```ini
rest_host    = 10.0.0.10        ; IIS host of DataCore REST Support
server_host  = dcs01            ; member of the SANsymphony server group
username     = monitor
password     = ********
scheme       = https            ; 'http' or 'https'
verify_tls   = false            ; ignore the certificate (true to validate)
timeout      = 30               ; per-call HTTP timeout in seconds
api_version  =                  ; '' (auto), '1.0', etc.
```

#### `[influxdb]`

```ini
url             = http://influxdb:8086
database        = datacore
username        = datacore
password        = ********
create_database = false         ; only set true if the user has admin rights
batch_size      = 500           ; lines per /write request
timeout         = 30
```

#### `[collector]`

```ini
interval_seconds = 30           ; matches DataCore's REST cache (RequestExpirationTime)
```

#### Per-category sections

Every category from the table above has its own section. Only the
`enabled` key is mandatory. Filter syntax:

```ini
[virtualdisks]
enabled          = true
include_names    = vd-prod-*
exclude_names    = *-tmp
include_counters = Total*, *Cache*
exclude_counters = TotalReadTime
```

- `include_names` / `exclude_names` filter resource instances by name
  (matches against `Caption`, `Alias`, `ExtendedCaption`, `HostName`,
  or `Name`, in that order of preference).
- `include_counters` / `exclude_counters` filter the metric (field)
  names returned by `/performance`.
- Patterns are case-insensitive, comma-separated, shell-style globs.
- Empty include list means *everything*; excludes always win.

---

## Operating the stack

### Starting and stopping

```bash
docker compose up -d           # start everything in the background
docker compose restart collector
docker compose stop            # graceful stop, keeps state
docker compose down            # stop + remove containers (keeps volumes)
docker compose down -v         # nuke volumes too (irreversible)
```

### Where the data lives

Three named volumes:

| Volume               | Used by                | Contents |
|----------------------|------------------------|---|
| `influxdb-data`      | `influxdb`             | Time-series database |
| `grafana-data`       | `grafana`              | Grafana state, users, modified dashboards |
| `collector-status`   | `collector` (rw), `setup` (ro) | `status.json` exposed via the setup UI |

The collector configuration (`collector/collector.ini`) and the
top-level `.env` are bind-mounted directly from the project tree, so
edits made on disk are visible to the containers immediately.

### Hot reload after editing the config

If you edit `collector.ini` directly:

```bash
docker compose kill -s SIGHUP collector
```

The collector finishes the current cycle, reloads `collector.ini`, and
keeps running. It does *not* restart, so the next cycle resumes with
the new settings within `interval_seconds`.

If you used the setup UI, the **Save** button can do this automatically
(checkbox *“Reload collector after saving”*).

### Logs

```bash
docker compose logs -f --tail=200 collector
docker compose logs -f setup
docker compose logs grafana | less
```

The setup UI has a richer live viewer at
<http://localhost:8088/logs> with level filtering, substring search,
pause and auto-scroll.

### Backups

The only stateful pieces are the named volumes. A simple snapshot:

```bash
docker compose down
docker run --rm -v grafana-datacore_influxdb-data:/data -v "$PWD:/out" \
  alpine tar czf /out/influxdb-backup.tgz -C /data .
docker run --rm -v grafana-datacore_grafana-data:/data -v "$PWD:/out" \
  alpine tar czf /out/grafana-backup.tgz -C /data .
docker compose up -d
```

For continuous backups, use InfluxDB's native `influxd backup`
sub-command; see the InfluxDB 1.12 documentation.

### Updating

```bash
git pull
docker compose pull             # latest base images for influxdb / grafana
docker compose build collector setup
docker compose up -d
```

The `collector` and `setup` images rebuild from the local sources;
`influxdb` and `grafana` are pulled from Docker Hub.

### Tuning the polling interval

DataCore's REST cache holds metrics for `RequestExpirationTime` seconds
(default **30 s**, set in `C:\Program Files\DataCore\Rest\Web.config`).
Polling more frequently than that yields the same data points twice.

For very large server groups, increase the interval to 60 s or more,
and consider disabling categories you don't need (the `physicaldisks`
loop in particular can be expensive when there are dozens of disks per
server).

### Reducing series cardinality

Each unique combination of measurement + tag values creates a series in
InfluxDB. With many vdisks or hosts this can grow quickly. Two levers:

- **Disable categories** you don't query.
- **Filter counters** with `include_counters` to only keep what your
  dashboards use.

---

## Setup UI

After `docker compose up -d`, browse to <http://localhost:8088>. Log
in with `SETUP_ADMIN_USER` / `SETUP_ADMIN_PASSWORD`.

### What the UI does

- **Edit** DataCore and InfluxDB credentials, scheme (`http`/`https`),
  TLS verification, and API version.
- **Test connection** for DataCore (calls `/servers` with the supplied
  credentials, exactly the way the collector does) and InfluxDB (calls
  `/ping` and `SHOW DATABASES`).
- **Toggle** each performance category on or off and edit the
  per-category include/exclude filters.
- **Save** writes both `collector/collector.ini` and the relevant keys
  in `.env`. Existing keys you have added to `.env` are preserved.
- **Reload** sends `SIGHUP` to the collector container via the Docker
  Engine API (the socket is mounted into the setup container).
- **Status panel** (top of the home page): polls `/status` every 5 s.
  Shows current state, cycle count, last cycle duration, points
  written, prefetched view of next cycle, and a per-category table of
  resources seen / kept / errors.
- **Logs viewer** at `/logs`: tails the last 500 lines and follows
  live via Server-Sent Events. Filter by substring or by minimum log
  level. Up to 5000 lines kept in memory.

### Security notes

- The setup container has the Docker socket mounted in order to send
  signals to the collector. **Treat this as a privileged capability.**
  Keep the setup port (`8088`) on a trusted network or behind a reverse
  proxy with additional authentication.
- The session cookie is signed with `SETUP_SECRET_KEY` from `.env`.
  When unset, a random key is generated at container startup, so
  sessions invalidate on every restart.
- Grafana, InfluxDB and Setup all default to `admin/admin` style
  passwords — change them in `.env` before exposing anything.

---

## Troubleshooting

### Collector logs say `404` on `/RestService/rest.svc/<resource>`

Some DataCore REST Support builds (2.0 / 2.01) require an `/1.0/`
prefix in the URL. The collector probes both forms and caches the
working one per endpoint, so you should see only one or two 404 lines
the first time, then nothing. If a category keeps returning 404, that
endpoint genuinely is not exposed by your build — disable it.

### `400 Bad Request: Pool Id cannot be empty.` on `/poollogicaldisks`

`/poollogicaldisks` cannot be listed naked: it requires a `?pool=<id>`
parameter. The collector enumerates the pools first and iterates per
pool. If you see this, it means the `pools` listing failed earlier in
the cycle — usually a network or authentication problem. Look for the
preceding `WARNING [datacore_collector] Could not list /pools` line.

### `403 Forbidden` on the InfluxDB write or `CREATE DATABASE`

The user the collector is using has no admin rights. Either grant them
admin (heavy) or set `create_database = false` in the `[influxdb]`
section: the database is created at first boot by the InfluxDB
container itself via `INFLUXDB_DB`.

### `Authentication rejected (401)` from DataCore

Three usual causes:

- Wrong password.
- The Windows user is not allowed in the SANsymphony group (use
  Computer Management on the REST Support host to confirm group
  membership).
- The `ServerHost` header points to a server not in the group: it must
  be the **member** server, not the IIS REST host (those can be the
  same or different machines).

### Grafana shows “No data” but Influx has rows

In Grafana's *Explore* with the `DataCore-InfluxDB` datasource, run
`SHOW MEASUREMENTS`. You should see `datacore_*` entries. If they are
absent, the collector hasn't written anything yet (check the Status
panel in the setup UI: `points_written` should be > 0 in the last
cycle). If they exist but the dashboard panels are empty, the panels'
time range may be ahead of your data — pick *Last 6 hours* and refresh.

### TLS certificate errors when `verify_tls = true`

Either install the certificate's CA on the collector host (out of
scope) or set `verify_tls = false`. The latter is fine on a private
management network and is the documented setup for self-signed REST
Support installs.

### The collector keeps restarting

Usually a typo in `collector.ini` or a missing required value. Look at
`docker compose logs collector` — the first lines should mention either
`Config file not found` or a `KeyError` from `configparser`.

---

## Migration notes from the original project

The upstream `lblanc/grafana-integration` image bundled everything
(sshd, chronograf, statsd, telegraf, older Grafana/InfluxDB) in a
single container. This fork drops:

- `sshd`, `statsd`, `chronograf` (use `docker exec` and Grafana's
  *Explore* tab instead),
- the bundled `telegraf` (replaced by the typed Python collector),
- the vSphere collection block (out of scope; pair with the official
  Telegraf vSphere input if you still need it).

The DataCore counters and the dashboard structure remain compatible.
Existing dashboards exported from the upstream project will work if
you change the measurement name from the original Telegraf-style
naming (e.g. `datacore_pool_perf`) to the new `datacore_pools`,
`datacore_virtualdisks`, etc. The field names returned by
`/performance` are unchanged — they come from DataCore directly.
