# Horizontal deployment: one Deployment per notebook

This is a working example of scaling tier 3 from
[`deploy/k8s/README.md`](../k8s/README.md): every notebook runs in its own
Deployment, the gateway is stateless and replicated, and the scheduler gets a
dedicated single-replica pod.

```
                         ┌──────────────────────────────────────────────┐
Browser ── Ingress ──────┤ default paths                                │
                         │   gallery-gateway ×2 (stateless)             │
                         │     GALLERY_BACKEND_URL_TEMPLATE=            │
                         │       http://nb-{slug}:2718                  │
                         │        ├─proxy─> nb-sales-dashboard :2718    │
                         │        ├─proxy─> nb-csv-explorer    :2718    │
                         │        └─proxy─> nb-cluster-lab     :2718    │
                         │                                              │
                         │ /schedules, /api/schedules, /runs,           │
                         │ /admin, /api/admin                           │
                         │   gallery-scheduler ×1 (PVC: gallery.db —    │
                         │     schedules AND users/groups — plus runs/) │
                         │     — executes `marimo export html`          │
                         │     subprocesses IN THIS POD                 │
                         └──────────────────────────────────────────────┘
```

## Deploy

```bash
# 1. Build and push the image, then update image: in the manifests.
# 2. Create the client-CA secret the ingress verifies certificates against:
kubectl create secret generic gallery-client-ca --from-file=ca.crt=your-ca.pem
# 3. Set GALLERY_ADMIN_DNS in gateway.yaml + scheduler.yaml, then:
kubectl apply -k deploy/horizontal_k8s/
```

## How it works

`GALLERY_BACKEND_URL_TEMPLATE=http://nb-{slug}:2718` flips the gateway into
external-backend mode (`src/gallery/manager.py`): it spawns no subprocesses,
skips the idle reaper, and the proxy resolves each notebook to its Service
DNS. Everything else — DN-header identity, login gating, the index, WebSocket
proxying — is unchanged. Identity is per-request (the ingress verifies the
client certificate and forwards the DN; see `ingress.yaml`), so the gateway
holds no session state at all and scales to any replica count with **no
session affinity** and no shared signing keys.

**Users/groups caveat.** Accounts, groups, and membership live in
`gallery.db`, and in this topology the only durable copy is on the scheduler
pod's PVC (which is why `/admin` routes there). Gateway replicas run with
empty local databases: `requires_login` gating works everywhere (it needs
only the header), but meta.yaml `groups:` gating at the gateway will only
admit `GALLERY_ADMIN_DNS` because the replicas can't see membership. If you
need group-gated notebooks with a replicated gateway, point the app at a
shared Postgres for users/groups/schedules — the persistence layer is
isolated in `src/gallery/db.py` for exactly that move.

Each `nb-*.yaml` runs `marimo run` directly as the container command with the
same flags the gateway would have used locally (`--base-url /apps/<slug>`,
health probe on `/apps/<slug>/health`). Per-notebook Deployments are the
scaling win: each notebook gets its own resource requests/limits (and an HPA
if you like), so a heavy notebook can't starve the others. To add a notebook,
copy an `nb-*.yaml`, replace the slug, and add it to `kustomization.yaml` —
the gateway picks it up by naming convention.

Trade-offs vs. the single-pod deployment: notebook pods are always on (no
lazy start / idle reaping — scale-to-zero would need something like KEDA),
and a new notebook needs a manifest, not just a directory drop.

## How scheduled runs fit in (read this before sizing pods)

**Scheduled runs do not execute in the nb-\* Deployments.** Those pods serve
interactive sessions only. The scheduler (`src/gallery/scheduler.py`) runs
each fire as a local `marimo export html` subprocess in whichever pod has
`GALLERY_SCHEDULES_ENABLED=true` — the backend resolver plays no part in
exports. Consequences, all encoded in these manifests:

- **`gallery-scheduler` is a dedicated single-replica Deployment.** If the
  scheduler ran in the replicated gateway, every replica would fire every
  schedule; if it ran alongside interactive traffic, a heavy export would
  compete with users for the same CPU/memory. Gateway replicas set
  `GALLERY_SCHEDULES_ENABLED=false`.
- **Size the scheduler pod for exports**: roughly
  `GALLERY_SCHEDULE_MAX_CONCURRENT_RUNS` × the footprint of your heaviest
  notebook's full execution. Sandboxed exports also build a uv env in this
  pod (the PVC-backed uv cache keeps that fast after the first run).
- **The scheduler pod owns the PVC** holding `gallery.db` (schedules, run
  history, users/groups) and `runs/<slug>/<run_id>/` artifacts. SQLite wants
  exactly one writer on a local filesystem — don't share it across pods via
  NFS/RWX.
- **The ingress routes `/schedules`, `/api/schedules`, `/runs`, and `/admin`
  to the scheduler pod** because that's where the database and artifacts
  live. Gateway replicas each open an empty local `gallery.db`, so schedule
  or admin traffic landing there would silently show users nothing — the
  path rules in `ingress.yaml` are load-bearing.
- Identity needs nothing shared between pods (the DN rides every request);
  schedule privacy and group sharing are enforced by the app against the
  scheduler's database, and each export subprocess receives the schedule
  creator's DN as `GALLERY_USER_DN` for on-behalf-of calls.

If you'd rather keep export execution off the long-lived pods entirely, the
k8s-native alternative is CronJobs running the same `marimo export html`
command (sketched in [`deploy/k8s/README.md`](../k8s/README.md)) — but those
runs bypass the gallery's schedule UI, run history, and retention.

## Network policy is part of auth here

Two policies in `networkpolicy.yaml`, both load-bearing:

- **Notebook pods accept traffic only from the gateway.** Login/group gating
  happens at the gateway proxy, not in the notebook pods — in-cluster, an
  nb-\* Service is otherwise open.
- **Gallery pods accept traffic only from the ingress controller.** The
  gateway and scheduler trust the DN header blindly; any pod that can reach
  them directly can claim any identity by setting the header itself. Adjust
  the namespace selector to your controller's labels.
