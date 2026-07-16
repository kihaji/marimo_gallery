# marimo notebook gallery

A self-hosted gallery where your team deploys [marimo](https://marimo.io)
notebooks as interactive apps. A FastAPI gateway serves a searchable index and
reverse-proxies each notebook to its own supervised `marimo run` process.

```
Browser ──> FastAPI gateway :8000
              ├── /                    gallery index (search / sort / tags / themes)
              ├── /apps/sales-dashboard ──proxy──> marimo run :10000
              ├── /apps/csv-explorer    ──proxy──> marimo run :10001
              └── /apps/cluster-lab     ──proxy──> marimo run :10002 (sandboxed)
```

- **Lazy lifecycle** — notebook processes start on first visit and are reaped
  after `GALLERY_IDLE_TTL_SECONDS` with no open sessions. Crashed processes
  restart on the next visit.
- **Isolation** — one process per notebook; each process serves all concurrent
  users of that notebook with an independent kernel session per user.
- **Shared code** — `src/gallery_shared/` is importable from every notebook
  (data loaders, plot theming, storage helpers).
- **Unique URLs** — every notebook lives at `/apps/<slug>/`.

## Run locally

```bash
uv sync --extra redis          # or plain `uv sync`
# No TLS proxy locally, so supply a fixed identity (see Authentication below):
export GALLERY_DEV_USER_DN="CN=Dev User,OU=Local,O=Example"
export GALLERY_ADMIN_DNS="$GALLERY_DEV_USER_DN"   # optional: make yourself admin
uv run uvicorn gallery.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000. Without `GALLERY_DEV_USER_DN` you browse
anonymously: only public notebooks are visible and the schedule pages 401. Run tests with `uv run pytest` (add `-m slow` for
the full subprocess lifecycle test).

### Docker

```bash
docker compose up --build                    # disk cache
docker compose --profile redis up --build    # + Redis cache (set REDIS_URL in compose)
```

### Kubernetes

Manifests and a scaling guide live in [`deploy/k8s/`](deploy/k8s/README.md).

## Adding a notebook

Drop a directory into `notebooks/` and restart the gateway:

```
notebooks/my-notebook/
├── app.py          # the marimo notebook
├── meta.yaml       # gallery card metadata
└── thumbnail.png   # optional 16:9 card image (SVG placeholder otherwise)
```

`meta.yaml`:

```yaml
title: My Notebook            # required
description: What it does.    # required
tags: [team-x, dashboard]     # required, at least one
sandbox: false                # true = run with `marimo run --sandbox` (see below)
include_code: false           # true = users can view notebook source
requires_login: false         # true = hidden/blocked unless the request has an identity
groups: [analytics]           # restrict to these groups (implies login; admins always pass)
session_ttl: 600              # optional marimo --session-ttl override
enabled: true                 # false hides it from the gallery
```

The directory name is the slug (`[a-z0-9-]`, becomes the URL). Broken entries
are logged and skipped — they never take the gallery down.

## Authentication & authorization

There are no passwords and no sessions. Identity is the **client-certificate
DN** your TLS-terminating proxy forwards on every request in the
`GALLERY_DN_HEADER` header (default `x-user-dn`):

```nginx
# Nginx terminating SSL with mandatory client certs:
proxy_set_header X-User-DN $ssl_client_s_dn;
```

**Trust model.** The gateway trusts that header unconditionally, so the proxy
must be the only network path to the gateway (in Kubernetes: a NetworkPolicy
admitting only ingress-controller traffic), and the proxy must always *set*
the header — never forward a client-supplied value. For local development
without a proxy, set `GALLERY_DEV_USER_DN` to act as a fixed user (used only
when the header is absent; a startup warning is logged).

**Accounts and groups.** A user row is created automatically the first time
a DN appears; users, groups, and membership live in `gallery.db` (the layer
is kept behind `src/gallery/db.py` for a later move to Postgres). Notebooks
declare `groups: [...]` in meta.yaml to restrict visibility to members —
authenticated non-members get 404s, so gated notebooks stay invisible —
while `requires_login: true` admits any authenticated user. The example
`csv-explorer` notebook is gated on the `analytics` group.

**Admins.** DNs listed in `GALLERY_ADMIN_DNS` (semicolon-separated — DNs
contain commas) become admins on first sight. Admins see every notebook and
get the `/admin` page: create groups, manage membership, grant or revoke
admin (revoking your own is blocked; env-granted admin is removed by editing
the env var). The page 404s for everyone else.

**User identity in notebooks.** The gateway forwards the DN to the notebook
backend on both the HTTP and WebSocket proxy paths, and scheduled exports
receive the schedule creator's DN as `GALLERY_USER_DN` in their environment.
Notebook code reads it with one call that works in both modes:

```python
from gallery_shared import identity

dn = identity.current_dn()   # full DN — for on-behalf-of calls, usage logging
who = identity.current_cn()  # display-friendly CN
```

See the banner cell in `notebooks/sales-dashboard/app.py`.

## Scheduled runs

Any logged-in user can schedule a notebook from its **Schedules** page (linked
on every gallery card): pick a cadence — every N hours, daily/weekly at a time,
or a raw cron expression — fill in the notebook's parameters, and the gateway
runs `marimo export html` at each fire. Every run produces a **viewable HTML
report** of all cell outputs plus a captured log, listed in the run history
with status and duration ("Run now" triggers an immediate run). Sandboxed
notebooks run with `--sandbox`.

**Schedules are private, with opt-in sharing.** Parameters (and the reports
they produce) may be sensitive, so by default users only see, edit, delete,
and run their own schedules, and run history/reports are visible only to
their creator — scheduled runs belong to whoever created the schedule, even
after the schedule is deleted. An owner may share a schedule **view-only**
with one of their groups: members then see the schedule and the runs it
produced (reports and logs included) but cannot edit, delete, run, or
re-share it, and the owner's ad-hoc "Run now" runs stay private. Deleting the
group reverts its shared schedules to private. The schedules pages always
require an identity, and scheduling a notebook requires the same group access
as opening it.

Schedules and history live in SQLite at `<storage_root>/gallery.db`; artifacts
under `<storage_root>/runs/<slug>/<run_id>/`. The newest
`GALLERY_SCHEDULE_RUNS_KEEP` runs are kept per schedule. Cron fires in the
**server's local timezone** (UTC in the container unless `TZ` is set); missed
fires while the gateway is down are skipped, not backfilled. A schedule whose
notebook is still running when it fires again skips that fire.

### Notebook parameters

Declare parameters in `meta.yaml` and they become a form on the Schedules page:

```yaml
parameters:
  - name: region          # ^[a-z][a-z0-9_]*$ — passed as --region <value>
    label: Region
    type: choice          # string | number | boolean | choice
    default: All
    choices: [All, North, South, East, West]
  - name: days
    type: number
    default: 90
```

Inside the notebook, read them with `mo.cli_args()` and fall back to the UI
controls so the same notebook works interactively and scheduled:

```python
cli_args = mo.cli_args()
region = cli_args.get("region") or region_multiselect.value
days = int(cli_args.get("days") or 90)
```

See `notebooks/sales-dashboard/app.py` for the full pattern (including a
banner cell so exported reports are self-describing). Values are validated
server-side against the declared types before being stored or passed.

## Dependency management

Two tiers:

1. **Shared environment (default).** `pyproject.toml` + `uv.lock` define one
   environment for the gateway, `gallery_shared`, and every non-sandboxed
   notebook. Add common packages here.
2. **Sandboxed notebooks.** Set `sandbox: true` in meta.yaml and declare
   [PEP 723](https://peps.python.org/pep-0723/) inline dependencies at the top
   of `app.py` (see `notebooks/cluster-lab/app.py`). marimo shells out to uv
   to build an isolated env — use this for heavy or conflicting deps. First
   launch is slow (packages resolve + download); the starting page covers it,
   and the uv cache makes later launches fast.

Sandboxed notebooks can still `import gallery_shared` because the gateway
injects `PYTHONPATH=<repo>/src`. **Production note:** publish
`gallery_shared` as a wheel to your internal index and reference it in each
sandboxed notebook's PEP 723 deps instead — PYTHONPATH injection is a PoC
convenience.

## Temporary file storage

`gallery_shared.storage` gives notebooks three tiers under
`$GALLERY_STORAGE_ROOT` (default `./data`):

| Helper | Path | Lifetime |
|---|---|---|
| `storage.scratch_dir()` | `scratch/<app>/<session>/` | deleted when the notebook process is reaped |
| `storage.save_upload(name, bytes)` | `uploads/<app>/` | survives reaps; filenames sanitized + collision-safe |
| `storage.get_cache(ns).get_or_compute(key, fn, ttl)` | `cache/<ns>/` or Redis | cross-session; Redis when `REDIS_URL` is set, disk otherwise, degrades gracefully |

Scheduled-run artifacts live beside these under `runs/<slug>/<run_id>/`, and
the schedules database is `<storage_root>/gallery.db`.

## Configuration

Environment variables (prefix `GALLERY_`, see `src/gallery/config.py`):

| Variable | Default | Meaning |
|---|---|---|
| `GALLERY_NOTEBOOKS_DIR` | `notebooks` | notebook directory to scan |
| `GALLERY_STORAGE_ROOT` | `./data` | root for scratch/uploads/cache |
| `GALLERY_IDLE_TTL_SECONDS` | `900` | reap a notebook after this much inactivity with no open sessions |
| `GALLERY_STARTUP_TIMEOUT_SECONDS` | `40` | max wait for a notebook to become healthy |
| `GALLERY_SANDBOX_STARTUP_TIMEOUT_SECONDS` | `300` | same, for sandboxed notebooks |
| `GALLERY_MARIMO_SESSION_TTL` | `600` | marimo's own per-client session TTL (keep ≤ idle TTL) |
| `GALLERY_MAX_UPLOAD_BYTES` | `104857600` | HTTP upload cap enforced at the proxy |
| `GALLERY_PORT_RANGE_START/END` | `10000`/`10999` | internal ports for notebook processes |
| `GALLERY_BACKEND_URL_TEMPLATE` | unset | e.g. `http://nb-{slug}:2718`: proxy to external per-notebook services instead of spawning (see `deploy/horizontal_k8s/`) |
| `GALLERY_SCHEDULES_ENABLED` | `true` | run the in-process scheduler (disable on extra replicas) |
| `GALLERY_SCHEDULE_TICK_SECONDS` | `20` | how often due schedules are checked |
| `GALLERY_SCHEDULE_MAX_CONCURRENT_RUNS` | `2` | scheduled/manual runs executing at once |
| `GALLERY_SCHEDULE_RUN_TIMEOUT_SECONDS` | `1800` | kill a run after this long (`3600` for sandbox via `..._SANDBOX_RUN_...`) |
| `GALLERY_SCHEDULE_RUNS_KEEP` | `20` | run history kept per schedule |
| `GALLERY_DN_HEADER` | `x-user-dn` | header the TLS-terminating proxy sets to the client-cert DN |
| `GALLERY_DEV_USER_DN` | unset | dev-only fixed identity when the header is absent — never set in production |
| `GALLERY_ADMIN_DNS` | unset | semicolon-separated DNs granted admin on first sight |
| `REDIS_URL` | unset | enables the Redis cache backend |

## Scaling under load

Short version: raise pod resources first; when one pod isn't enough, split
each notebook into its own Deployment behind the gateway (set
`GALLERY_BACKEND_URL_TEMPLATE` — built in) so notebooks scale independently
and the gateway becomes stateless. Multi-replica with session affinity is
possible but fragile for stateful sessions. Full discussion in
[`deploy/k8s/README.md`](deploy/k8s/README.md); a complete per-notebook
example including the dedicated scheduler pod is in
[`deploy/horizontal_k8s/`](deploy/horizontal_k8s/README.md).

## Security notes for production

- The DN header is trusted blindly, so lock the network path: only the
  TLS-terminating proxy may reach the gateway, the proxy must require client
  certificates and always overwrite the header, and `GALLERY_DEV_USER_DN`
  must never be set. Anyone who can reach the gateway directly can be anyone.
- `include_code: false` (the default) keeps notebook source off the client.
- Notebook code runs with the gateway's privileges — treat the notebooks
  directory as code review territory, and run the container as the provided
  non-root user.
