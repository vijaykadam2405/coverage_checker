# Deploying to the data-centre server

The crawler stack (Layer 1) running on the remote box and writing into the existing **`mallory-data`**
Postgres + MinIO. Everything here is host/port wiring — **no secret is stored in this repo**; see
[Credentials](#credentials) for the two values you paste on the server.

---

## 1 · The server

| | |
|---|---|
| Host | `$DATA_CENTRE_HOST` |
| SSH port | `$DATA_CENTRE_SSH_PORT` |
| User | `$DATA_CENTRE_SSH_USER` |
| Auth | key-based — **the default key is not authorized**, pass your deploy key explicitly |

```bash
ssh -i ~/.ssh/<your-deploy-key> -p "$DATA_CENTRE_SSH_PORT" "$DATA_CENTRE_SSH_USER@$DATA_CENTRE_HOST"
```

> The key itself is deliberately not in this repo and not named here. Use whichever private key you
> registered for this box.

**Capacity:** 40 cores · 1007 GB RAM · 20 TB disk (Ubuntu, Docker + Compose).
**Stay within the authorised budget: 30 cores / 500 GB RAM.** The box has co-tenants.

---

## 2 · What is already on the box — do not disturb

`mallory-data` is an **existing** compose stack. It owns the corpus and must not be recreated:

- **Postgres 16** — `127.0.0.1:5432`, volume `mallory-data_pgdata`
- **MinIO** — `127.0.0.1:9000`, volume `mallory-data_miniodata`
- Config lives in `~/mallory-data/`

Both bind to **loopback only**. A `DOCKER-USER` firewall rule (`~/mallory-firewall.sh`,
systemd `mallory-firewall.service`) DROPs external traffic to 5432/9000 from everything except the
authorised crawler IP.

**Other tenants (leave alone):** `smartcrawler-*`, `deploy-p0-*`, `p0_deploy`, `p1-crawler`,
`research`, `gold`.

### Port map

| Taken (do not bind) | Free at last check |
|---|---|
| 22, 5432, 8080, 8088, 8090, 9000, 9002 | 8095, 9003, 5434, 9377, 9378 |

Publish dashboards on `127.0.0.1` only and reach them over an SSH tunnel — **do not expose to the
internet.** To view a dashboard locally:

```bash
ssh -i ~/.ssh/<your-deploy-key> -p $DATA_CENTRE_SSH_PORT -L 8099:127.0.0.1:8099 $DATA_CENTRE_SSH_USER@$DATA_CENTRE_HOST
```

---

## 3 · Deploy

```bash
# on the server
git clone <this repo> ~/mallory-crawler
cd ~/mallory-crawler
cp .env.example .env
$EDITOR .env                 # fill in the DATA CENTRE block (section 4)
docker compose -f docker-compose.yml -f docker-compose.datacentre.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.datacentre.yml ps
```

**Always pass both files.** `docker-compose.datacentre.yml` is what makes this safe on a shared box,
and it does three things the base file cannot:

- keeps the bundled Postgres and MinIO **down**, so the crawler writes into the real corpus instead
  of a second, empty database that would look perfectly healthy while nothing deduped;
- binds every port to `127.0.0.1` — the base file publishes 8090/8099/9090/9377 on `0.0.0.0`, which
  here would collide with a co-tenant and put a dashboard carrying a Flush button on the internet;
- caps the four services at **30 CPUs / 496 GB** between them. Docker imposes no limit by default,
  and the first symptom of that is somebody else's container being OOM-killed.

The dashboard listens on **8091** server-side; 8090 belongs to a co-tenant. See section 9 for the
tunnel that puts it back on `localhost:8090`.

Because `mallory-data` already provides Postgres and MinIO, **do not start the bundled `postgres` /
`minio` containers.** Point the crawler at the existing ones (next section) and bring up only the
crawler services.

---

## 4 · Wiring to `mallory-data`

Uncomment the **DATA CENTRE** block in `.env`. The non-secret values are already filled in:

```ini
DATABASE_URL=postgresql://USER:PASSWORD@172.17.0.1:5432/mallory
CRAWLER_MINIO_ENDPOINT=172.17.0.1:9000
MINIO_BUCKET=mallory-raw
MINIO_SECURE=0
MINIO_ACCESS_KEY=          # paste on the server
MINIO_SECRET_KEY=          # paste on the server
```

`172.17.0.1` is the Docker bridge gateway — how a container reaches a service the host has bound to
`127.0.0.1`. Using `localhost` here would resolve to the *container*, not the host, and fail.
`MINIO_SECURE=0` because the hop is loopback-local, not the public internet.

Confirm the two services are reachable from inside a container before a full run:

```bash
docker run --rm alpine sh -c 'nc -z 172.17.0.1 5432 && echo pg-ok; nc -z 172.17.0.1 9000 && echo minio-ok'
```

---

## 5 · Credentials

`DATA_CENTRE_HOST` / `DATA_CENTRE_SSH_PORT` / `DATA_CENTRE_SSH_USER` — the address of your
centre. Kept out of the repo on purpose: this is a public repository, and an IP plus a
non-standard SSH port plus a username is a complete target triple that removes whatever
the unusual port was buying you and leaves only the key as defence.


**Nothing secret is committed to this repository, and nothing secret should be.** This repo is
public; anything committed here is permanently in the git history even if deleted later.

You supply two values, on the server only, in `.env` (which `.gitignore` already excludes):

| Value | Where to find it |
|---|---|
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | the running `mallory-data` MinIO — `~/mallory-data/.env` (or `docker compose config` in that directory) |
| `DATABASE_URL` user/password | the same `~/mallory-data/` config |

Verify they never get committed:

```bash
git check-ignore -v .env     # must print a .gitignore rule
git status --porcelain       # .env must NOT appear
```

---

## 6 · Sizing for this box

Defaults in `.env.example` are tuned for a 24-core workstation. This machine has 40 cores, but the
budget is 30 — and more browser tabs is **not** more throughput. Tabs are only used for renders, and
render concurrency is capped separately, so extra tabs just add Chromium processes that starve the
renders you actually want.

```ini
CRAWLER_BROWSERS=12
CRAWLER_TABS_PER_BROWSER=8     # 96 tabs ≈ 2.4/core against the 30-core budget
CRAWLER_TAB_POOL=1             # tab-less workers do httpx; tabs are borrowed only to render
CRAWLER_WORKERS_N=256          # httpx concurrency, independent of tab count
```

Watch `load average` against your core budget after the first run; if it climbs past ~30, lower
`CRAWLER_BROWSERS` before anything else.

---

## 7 · Health check

```bash
curl -s localhost:8099/v1/metrics | jq '{pool, batch, cpu_percent}'
docker compose logs -f crawler-api | grep -iE "engine started|tab pool"
```

Expect `engine started: 12 browsers x 8 tabs` and `tab pool: 256 workers over 96 tabs`.

---

## 8 · What was removed

There used to be a separate **fleet dashboard** at `~/mallory-fleet/` (:8097) whose job was to show
several crawling machines reporting into one centre. The crawler now runs ON the centre, so there is
one node, and its own dashboard already shows what that page did. It has been removed — the stack,
the compose file and the read-only role are no longer needed, and `FLEET_HEARTBEAT` now defaults to
`0` so nothing keeps writing `fleet_nodes` / `fleet_samples` rows that nothing reads.

The existing rows in those two tables are left alone. They are telemetry, they are small, and
deleting data on this box is never a side effect of a deployment.

If a second crawling machine is ever added, the writer is still in the image: set `FLEET_HEARTBEAT=1`
and `FLEET_DSN`, and give every machine its own `MALLORY_NODE_ID`.
