# grafana-datacore

A Docker-Compose stack that pulls performance counters from the
**DataCore SANsymphony REST API** and visualises them in Grafana via
InfluxDB. Modernised fork of
[`lblanc/grafana-integration`](https://github.com/lblanc/grafana-integration):

- Four lean services (Grafana, InfluxDB, Python collector, FastAPI setup
  UI) instead of one monolithic container with `supervisord`.
- Up-to-date base images: **Grafana OSS 13**, **InfluxDB 1.12-alpine**,
  **Python 3.12-slim**.
- Type-hinted Python 3 collector with a single dependency (`requests`).
- Auto-detection of DataCore's REST URL layout (versionless vs. `/1.0/`).
- Web setup UI with **Test connection**, hot **reload**, **status**
  panel and a live **logs** viewer.

---

## Table of contents

1. [Architecture](#architecture)
2. [What gets collected](#what-gets-collected)
3. [Deployment](#deployment)
4. [Configuration reference](#configuration-reference)
5. [Operating the stack](#operating-the-stack)
6. [Setup UI](#setup-ui)
7. [Dashboard](#dashboard)
8. [Troubleshooting](#troubleshooting)
9. [Migration notes from the original project](#migration-notes-from-the-original-project)

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

- **`influxdb`** — time-series store (InfluxDB 1.12-alpine). The
  `INFLUXDB_DB` variable creates the database at first boot, and a
  non-admin user is provisioned for the collector and Grafana.
- **`grafana`** — Grafana OSS 13 with the InfluxDB datasource and the
  starter `DataCore Overview` dashboard provisioned automatically.
- **`collector`** — Python 3.12 service that polls DataCore on a timer
  and writes line-protocol points to InfluxDB. Runs as UID 10001.
  Writes its current state to `/status/status.json` on a shared volume.
- **`setup`** — FastAPI web UI to edit the configuration, run
  connectivity tests, hot-reload the collector via `SIGHUP`, view the
  collector status and stream its logs.

## What gets collected

For every enabled resource category, the collector calls
`/RestService/rest.svc[/1.0]/<category>` to enumerate the resources,
then `/RestService/rest.svc[/1.0]/performance/<Id>` for each one.
Numeric counters become InfluxDB **fields**, descriptive properties
become **tags**, and selected resource-level state attributes (`State`,
`PoolStatus`, `DiskStatus`, `Size`, `CacheSize`, …) are also emitted
as fields so dashboards can do value-mapping (Online/Offline, etc.).

| Section name           | DataCore endpoint        | Notes |
|------------------------|--------------------------|---|
| `servers`              | `/servers`               | DataCore servers metrics + status |
| `servergroups`         | `/servergroups`          | Group-level metrics |
| `pools`                | `/pools`                 | Disk pool I/O and capacity |
| `poolmembers`          | `/poolmembers`           | Per-disk-in-pool metrics |
| `poollogicaldisks`     | `/logicaldisks?pool=…`   | Listed per pool — handled automatically |
| `physicaldisks`        | `/physicaldisks`         | All physical disks |
| `sharedpools`          | `/sharedpools`           | Recommended over `pools` if shared SAN |
| `sharedphysicaldisks`  | `/sharedphysicaldisks`   | Same idea for disks |
| `virtualdisks`         | `/virtualdisks`          | Virtual disks served to hosts |
| `virtualdiskgroups`    | `/virtualdiskgroups`     | Virtual disk groups |
| `virtuallogicalunits`  | `/virtuallogicalunits`   | Path-level metrics |
| `logicaldisks`         | `/logicaldisks`          | Pass-through sources, off by default |
| `hosts`                | `/hosts`                 | Host-side initiator metrics |
| `hostgroups`           | `/hostgroups`            | Host group aggregates |
| `scsiports`            | `/scsiports`             | Off by default — not exposed on REST 2.x |
| `targetdevices`        | `/targetdevices`         | Off by default |
| `snapshots`            | `/snapshots`             | Off by default |
| `snapshotgroups`       | `/snapshotgroups`        | Off by default |
| `rollbackgroups`       | `/rollbackgroups`        | Off by default |

### InfluxDB schema

- One measurement per category: `datacore_<category>` (e.g.
  `datacore_virtualdisks`, `datacore_pools`).
- **Tags**: `category`, `resource_id`, `resource_name`, plus any of
  `caption`, `extendedcaption`, `alias`, `hostname`, `servername`,
  `groupname`, `poolname`, `hostid`, `serverid`, `serverhostid` when
  present on the resource.
- **Fields** — two sources combined:
  - Numeric counters from `/performance` (e.g. `TotalBytesRead`,
    `TotalOperations`, `BytesAllocated`, `BytesOverSubscribed`,
    `CacheReadHits`, …).
  - Numeric **resource-level attributes** for status panels:
    `State`, `Status`, `PoolStatus`, `DiskStatus`, `CacheState`,
    `PowerState`, `Size`, `CacheSize`, `ChunkSize`, `MaxTierNumber`,
    `TierReservedPct`, `Type`. DataCore's `{Value, Units}` wrapped
    quantities are unwrapped automatically.
- **Timestamp**: the `CollectionTime` returned by DataCore (millisecond
  precision).

### Known DataCore quirks handled by the collector

- **REST URL layout varies per build.** Listing endpoints accept either
  `/RestService/rest.svc/pools` or `/RestService/rest.svc/1.0/pools`;
  performance endpoints want the resource ID as a path segment
  (`/performance/<id>`), not as a query string. The collector probes
  both forms once per endpoint and caches the working one.
- **Custom auth header.** DataCore uses a non-standard
  `Authorization: Basic <user> <password>` (no base64), and a
  `ServerHost: <name>` header to target a specific server in the group.
- **uint64 sentinels.** Counters like `EstimatedDepletionTime` can
  return `2^64 - 1` ("never"). InfluxDB 1.x stores int64 signed and
  rejects out-of-range values; the collector skips those fields rather
  than break the whole batch.
- **Dict-wrapped values.** `Size`, `MaxPoolBytes`, etc. are returned
  as `{"Value": N, "Units": "Bytes"}`. The collector unwraps the inner
  `Value`.
- **/poollogicaldisks** isn't listable directly. It maps to
  `/logicaldisks?pool=<id>` per pool.

---

## Deployment

### Prerequisites

- Linux host with **Docker Engine 24+** and **docker compose v2**.
- Network reachability from the host to the DataCore REST Support
  server on port 80 (HTTP) or 443 (HTTPS).
- A DataCore user with read access to the REST API. Any standard
  Windows user that the SANsymphony group recognises will do.

### 1. Clone the repository

```bash
git clone https://github.com/lblanc/grafana-datacore.git
cd grafana-datacore
```

### 2. Create the `.env`

```bash
cp .env.example .env
$EDITOR .env
```

> **Don't `docker compose up` before this.** If `.env` doesn't exist,
> Docker creates it as a *directory* and the stack fails to start.
> If that happens: `rm -rf .env && cp .env.example .env`.

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

You can keep most defaults for an evaluation deployment. **Change every
`changeme` before exposing the stack to anything beyond your laptop.**

### 3. Adjust `collector/collector.ini` if needed

Defaults enable the most useful categories. The most important field
is the **transport choice**:

```ini
[datacore]
scheme       = https      ; or 'http'
verify_tls   = false      ; set to true if you have a trusted certificate
api_version  =            ; leave empty to auto-detect; set '1.0' for REST 2.x
```

You can also edit categories from the web setup UI without touching
the file by hand.

### 4. Build and start

```bash
docker compose build
docker compose up -d
docker compose ps
```

You should see four healthy containers. Initial pulls and image builds
take 1–3 minutes depending on bandwidth.

### 5. First-run validation

```bash
# Tail the collector — within ~30s you should see lines like
#   "Wrote N points to InfluxDB database 'datacore'"
docker compose logs -f collector
```

URLs after a successful start:

| Service       | URL                       | Default credentials |
|---------------|---------------------------|---|
| Grafana       | http://localhost:3000     | `GF_ADMIN_USER` / `GF_ADMIN_PASSWORD` |
| Setup UI      | http://localhost:8088     | `SETUP_ADMIN_USER` / `SETUP_ADMIN_PASSWORD` |
| InfluxDB API  | http://localhost:8086     | `INFLUX_*` |

### 6. Open the starter dashboard

In Grafana, navigate to *Dashboards → DataCore → DataCore Overview*.
The pre-provisioned datasource is `DataCore-InfluxDB` and the dashboard
queries the `datacore_*` measurements directly.

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
current cycle finishes).

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

Every category has its own section. Only the `enabled` key is
mandatory. Filter syntax:

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
| `collector-status`   | `collector` (rw), `setup` (ro) | `status.json` consumed by the setup UI |

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
(checkbox *"Reload collector after saving"*).

### Logs

```bash
docker compose logs -f --tail=200 collector
docker compose logs -f setup
docker compose logs grafana | less
```

The setup UI has a richer live viewer at <http://localhost:8088/logs>
with level filtering, substring search, pause and auto-scroll.

When InfluxDB rejects a batch, the collector also dumps the full error
message and the offending request body to `/app/dumps/rejected-*.txt`
inside the collector container — useful when Docker log truncation
would cut off the relevant part.

### Backups

The only stateful pieces are the named volumes:

```bash
docker compose down
docker run --rm -v grafana-datacore_influxdb-data:/data -v "$PWD:/out" \
  alpine tar czf /out/influxdb-backup.tgz -C /data .
docker run --rm -v grafana-datacore_grafana-data:/data -v "$PWD:/out" \
  alpine tar czf /out/grafana-backup.tgz -C /data .
docker compose up -d
```

For continuous backups, use InfluxDB's native `influxd backup`
sub-command.

### Updating

```bash
git pull
docker compose pull             # latest base images for influxdb / grafana
docker compose build collector setup
docker compose up -d
```

### Tuning the polling interval

DataCore's REST cache holds metrics for `RequestExpirationTime` seconds
(default **30 s**, set in `C:\Program Files\DataCore\Rest\Web.config`).
Polling more frequently than that yields the same data points twice.

For very large server groups, increase the interval to 60 s or more,
and consider disabling categories you don't need (`physicaldisks` in
particular can be expensive when there are dozens of disks per server).

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

### Capabilities

- **Edit** DataCore and InfluxDB credentials, scheme (`http`/`https`),
  TLS verification, and API version (with auto-detect placeholder).
- **Test connection** for DataCore (calls `/servers` exactly the way
  the collector does) and InfluxDB (calls `/ping` and `SHOW DATABASES`).
- **Toggle** each performance category on or off and edit the
  per-category include/exclude filters.
- **Save** writes both `collector/collector.ini` and the relevant keys
  in `.env`. Existing keys you have added to `.env` are preserved.
- **Reload** sends `SIGHUP` to the collector container via the Docker
  Engine API (the socket is mounted into the setup container).
- **Status panel** (top of the home page): polls `/status` every 5 s.
  Shows current state, cycle count, last cycle duration, points
  written, next cycle countdown, and a per-category table of resources
  seen / kept / errors.
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

## Dashboard

The provisioned `DataCore Overview` dashboard (uid `datacore-overview`)
contains four sections:

- **Overview** (always expanded): server-group-wide IOPS and throughput
  (current value + read/write timeseries), per-pool allocation
  bargauge, capacity donut by pool.
- **DataCore Servers — $server** (one row per selected server,
  expanded): state / cache / power state pills (Online/Offline/AC OK
  with value-mapped colours), cache size, cache hit ratio, bytes
  migrated, server-aggregated IOPS and latency.
- **DataCore Pool — $pool** (one row per selected pool, expanded):
  pool status, total/used/free, oversubscription, allocation gauge,
  capacity breakdown donut, IOPS and latency timeseries.
- **Virtual Disk: $vdisk** (one row per top-N vdisk, expanded): state,
  size, allocation donut, IOPS timeseries (Total / Reads / Writes).

### Variables

| Name      | Type   | Behaviour |
|-----------|--------|---|
| `$server` | query, multi | All DataCore servers, defaults to `All` |
| `$pool`   | query, multi | All disk pools, defaults to `All` |
| `$vdisk`  | query, multi | Top N virtual disks by IOPS over the current time range, defaults to `All`. The list is dynamic — change `$top` and the picker refreshes. |
| `$top`    | custom | 5 / 10 / 20 / 50 |

### InfluxDB query patterns used

- **Aggregated rates** use the per-series-derivative-then-sum pattern
  (`SELECT sum(rate) FROM (SELECT non_negative_derivative(...) AS rate
  ... GROUP BY tag, time)`). Doing `non_negative_derivative(sum(...))`
  the other way round produces spurious huge spikes when the number of
  series varies between buckets.
- All panels using `non_negative_derivative` set `interval: "1m"` as
  the minimum query interval. With a 30 s collection cadence,
  `$__interval` below ~1 minute leaves only one point per bucket and
  the derivative can't compute, leading to empty graphs.
- **Latency** is computed as `delta_time / delta_ops` inside a
  subquery, then `mean()` is applied outside. The straight form
  `non_negative_derivative(time) / non_negative_derivative(ops)` does
  not work in InfluxQL 1.x.
- The `$vdisk` variable uses the proven InfluxDB 1.x top-N pattern:
  `SELECT resource_name FROM (SELECT top(iops, resource_name, $top)
  FROM (SELECT sum(iops) AS iops FROM (...) GROUP BY resource_name))`.
  The outer `SELECT resource_name` is essential — without it Grafana
  picks up the IOPS values as variable values instead of the names.

---

## Troubleshooting

### `.env` was created as a directory

Happened because `docker compose up` ran before `cp .env.example .env`.
Recover with:

```bash
docker compose down
rm -rf .env
cp .env.example .env
$EDITOR .env
docker compose up -d
```

### Collector logs say `404` on `/RestService/rest.svc/<resource>`

Some DataCore REST Support builds (2.0 / 2.01) require an `/1.0/`
prefix in the URL. The collector probes both forms and caches the
working one per endpoint, so you should see only one or two 404 lines
the first time, then nothing. If a category keeps returning 404, that
endpoint genuinely is not exposed by your build — disable it.

### `400 Bad Request: Pool Id cannot be empty.` on `/poollogicaldisks`

The `/poollogicaldisks` endpoint doesn't actually exist on REST 2.x —
it's exposed as `/logicaldisks?pool=<id>`. The collector handles this
automatically.

### `unable to parse integer 18446744073709551615: value out of range`

DataCore returns `2^64 - 1` as a "never" sentinel for some counters
(typically `EstimatedDepletionTime`). InfluxDB 1.x stores signed int64
and rejects this value. The collector skips out-of-range integers; if
you still see this error, rebuild the collector image (`docker compose
build --no-cache collector`).

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

### Grafana panel shows "No data" but Influx has rows

Run the panel's query directly against InfluxDB to see what comes back:

```bash
INFLUX_USER=$(grep '^INFLUX_USER=' .env | cut -d= -f2)
INFLUX_PASSWORD=$(grep '^INFLUX_PASSWORD=' .env | cut -d= -f2)
INFLUX_DB=$(grep '^INFLUX_DB=' .env | cut -d= -f2)

docker compose exec influxdb influx -username "$INFLUX_USER" \
  -password "$INFLUX_PASSWORD" -database "$INFLUX_DB" \
  -execute 'SHOW FIELD KEYS FROM "datacore_pools"'
```

Common cases:

- The dashboard panel references a field that isn't in the data. Check
  that the field appears in `SHOW FIELD KEYS`. Status fields like
  `State` only appear after the resource-level field unwrapping
  introduced in this fork — if they're missing, rebuild the collector.
- The time range is shorter than the collector's interval. Below ~2
  minutes there aren't enough points for `non_negative_derivative` to
  compute a rate.
- `$server` / `$pool` / `$vdisk` selection doesn't match any series.
  Try `All` first.

### Grafana fails to start with "Datasource provisioning error: data source not found"

Grafana 13 validates provisioning more strictly than older versions.
The dashboard datasource UID is `datacore_influxdb`. If you upgraded
from an earlier setup with a different UID, wipe the Grafana volume:

```bash
docker compose down
docker volume rm grafana-datacore_grafana-data
docker compose up -d
```

You won't lose anything — the dashboards are provisioned from disk on
each start.

### Collector status card says "Status file not found"

The collector writes `/status/status.json` to the `collector-status`
volume; the setup UI reads it. Two possible causes:

- The collector hasn't completed its first cycle yet. Wait 30 s.
- The volume was created with root ownership before the collector
  Dockerfile was updated. Recreate it:
  ```bash
  docker compose down
  docker volume rm grafana-datacore_collector-status
  docker compose up -d
  ```

### TLS certificate errors when `verify_tls = true`

Either install the certificate's CA on the collector host (out of
scope) or set `verify_tls = false`. The latter is fine on a private
management network and is the documented setup for self-signed REST
Support installs.

### The collector keeps restarting

Usually a typo in `collector.ini` or a missing required value. Check
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

### Schema differences from the original

The original project used `telegraf` with measurement names like
`DataCore_Servers`, `DataCore_Virtual_Disks`, `DataCore_Disk_pools`
and tags `objectname` / `host` / `instance`. This fork uses:

- Measurement names: lowercased and snake-cased, prefixed with
  `datacore_` (e.g. `datacore_virtualdisks`, `datacore_pools`).
- Tags: `category`, `resource_id`, `resource_name`, plus optional
  resource-level tags (`caption`, `extendedcaption`, `alias`,
  `serverid`, …) — no more `objectname` / `host` / `instance`.

Old dashboards exported from the upstream project will not work
as-is; the included `DataCore Overview` dashboard is rewritten for the
new schema.