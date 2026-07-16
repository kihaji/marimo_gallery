# Kubernetes deployment & scaling guide

```bash
kubectl apply -f deployment.yaml -f service.yaml   # add pvc.yaml if desired
```

Expose the Service through your ingress. **WebSockets must be allowed end to
end** — marimo's kernel protocol runs over WS. For nginx-ingress, long-lived
connections need generous timeouts:

```yaml
nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
```

## How load behaves

One gateway pod runs the index plus **one marimo subprocess per active
notebook**. Each subprocess handles all concurrent user sessions for that
notebook (each user session gets its own Python kernel inside that process).
Memory therefore grows with *(active notebooks) × (sessions × their data)*,
and CPU with whatever the notebooks compute. Idle notebooks are reaped after
`GALLERY_IDLE_TTL_SECONDS`, so the footprint tracks actual usage.

## Scaling strategies, in order of effort

### 1. Vertical (start here)

Raise the pod's resource limits. Simple and effective until a single node
can't hold the working set. Watch container memory: the kernel sessions, not
the gateway, are what grow.

### 2. Replicas + session affinity (fragile — read first)

`replicas: 2+` works only if every session stays pinned to one pod, because a
notebook session is state inside that pod's subprocess:

- Set `Service.sessionAffinity: ClientIP`, or cookie affinity at the ingress
  (`nginx.ingress.kubernetes.io/affinity: cookie`).
- A rescheduled or scaled-down pod kills the kernels it hosts; users lose
  session state mid-run.
- Uploads and scratch diverge per pod (each has its own emptyDir). Use Redis
  for anything that must be shared.

Acceptable for read-mostly dashboards; not recommended for long-running
stateful sessions.

### 3. Per-notebook Deployments (the real horizontal path)

Split each notebook into its own Deployment + Service running
`marimo run <app.py> --host 0.0.0.0 --port 2718 --base-url /apps/<slug>`, e.g.
service `nb-sales-dashboard`. This mode is built in: set

```yaml
- name: GALLERY_BACKEND_URL_TEMPLATE
  value: "http://nb-{slug}:2718"
```

and the gateway stops spawning subprocesses entirely — the proxy resolves
each notebook to its Service DNS (`ProcessManager.base_url()` in
`src/gallery/manager.py`) and the idle reaper is skipped. Then:

- The gateway becomes stateless → scale it freely behind a plain Service, no
  session affinity.
- Each notebook gets its own resource requests/limits and an HPA; a heavy
  notebook can't starve the others.
- Session affinity is then only needed per notebook Service if a notebook
  itself runs multiple replicas.

This is the recommended production topology once more than a handful of teams
share the gallery. **A complete worked example — gateway, all three
notebooks, dedicated scheduler pod, ingress routing, network policy — lives
in [`deploy/horizontal_k8s/`](../horizontal_k8s/README.md).**

## Scheduled runs

The scheduler runs **in-process in the gateway**. Two consequences:

1. **One active scheduler.** With `replicas: 2+`, every pod would fire every
   schedule. Set `GALLERY_SCHEDULES_ENABLED=false` on all but one replica
   (e.g. a separate single-replica "scheduler" Deployment of the same image),
   or move execution out of the gateway entirely with k8s CronJobs running
   the same command against the shared volume:

   ```yaml
   apiVersion: batch/v1
   kind: CronJob
   metadata: { name: nightly-sales-report }
   spec:
     schedule: "0 2 * * *"
     jobTemplate:
       spec:
         template:
           spec:
             containers:
               - name: run
                 image: marimo-gallery:latest
                 command: ["python", "-m", "marimo", "export", "html",
                           "notebooks/sales-dashboard/app.py",
                           "-o", "/data/runs/sales-dashboard/manual/report.html",
                           "--no-include-code", "--",
                           "--region", "West", "--days", "30"]
                 volumeMounts: [{ name: data, mountPath: /data }]
             restartPolicy: Never
             volumes: [{ name: data, persistentVolumeClaim: { claimName: marimo-gallery-data } }]
   ```

   (CronJob-produced runs won't appear in the gallery's run history — that
   requires the in-process scheduler.)

   Note that scheduled runs always execute as `marimo export html`
   subprocesses **inside the scheduler's own pod** — even in the
   per-notebook-Deployment topology, where the nb-* pods serve only
   interactive sessions. Size the scheduler pod accordingly; see the
   [horizontal example](../horizontal_k8s/README.md) for the full pattern.

2. **Persist `/data`.** Schedules and run history live in
   `<storage_root>/gallery.db` with artifacts under `runs/` — use the PVC, not
   an emptyDir, if schedules must survive pod rescheduling.

## Storage mapping

| Path under /data | Purpose | Backing |
|---|---|---|
| `scratch/` | per-session temp files, cleaned on reap | emptyDir |
| `uploads/` | user-uploaded files | emptyDir, or PVC to survive restarts |
| `cache/` | cross-session disk cache | PVC — or set `REDIS_URL` and let the shared lib use Redis |
| `uv-cache/`, `uv-python/` | sandbox package cache | PVC strongly recommended: keeps `--sandbox` cold starts fast |

`emptyDir.sizeLimit` (set in deployment.yaml) makes a runaway upload evict the
pod predictably instead of filling the node.

## Auth

Identity is the **client-certificate DN** forwarded per-request in
`GALLERY_DN_HEADER` — there are no passwords, sessions, or shared keys, so
nothing auth-related needs to be synchronized between replicas. Two pieces
must be in place:

1. **The ingress terminates TLS with mandatory client certs and injects the
   DN.** With ingress-nginx, CA verification makes the controller forward
   `ssl-client-subject-dn` upstream:

   ```yaml
   nginx.ingress.kubernetes.io/auth-tls-verify-client: "on"
   nginx.ingress.kubernetes.io/auth-tls-secret: "default/gallery-client-ca"
   ```

   and the gallery pods set `GALLERY_DN_HEADER=ssl-client-subject-dn`. If
   your own Nginx fronts the cluster instead, keep the default
   `x-user-dn` and configure `proxy_set_header X-User-DN $ssl_client_s_dn;`.

2. **Only the ingress may reach the gallery pods.** The header is trusted
   blindly; a NetworkPolicy restricting ingress to the controller's namespace
   is load-bearing for auth, not hygiene (see
   [`../horizontal_k8s/networkpolicy.yaml`](../horizontal_k8s/networkpolicy.yaml)).

Accounts are auto-created on first sight of a DN; set `GALLERY_ADMIN_DNS`
(semicolon-separated) to bootstrap admins, who manage groups at `/admin`.
Users, groups, and membership live in `gallery.db` on the pod's volume —
fine for this single-pod topology; see the horizontal README for what that
means with replicated gateways. Keep `/healthz` unauthenticated for probes
(it already is).
