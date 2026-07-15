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
service `nb-sales-dashboard`. Then:

- The gateway stops spawning subprocesses; `ProcessManager.base_url()` (see
  `src/gallery/manager.py`, the "backend resolution" seam) returns
  `http://nb-<slug>:2718` instead. Everything else is unchanged.
- The gateway becomes stateless → scale it freely behind a plain Service.
- Each notebook gets its own resource requests/limits and an HPA; a heavy
  notebook can't starve the others.
- Session affinity is then only needed per notebook Service if a notebook
  itself runs multiple replicas.

This is the recommended production topology once more than a handful of teams
share the gallery.

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

The gateway is unauthenticated in this PoC. Add OIDC either at the ingress
(e.g. oauth2-proxy) or as ASGI middleware at the marked slot in
`src/gallery/main.py` — every route including the WebSocket proxy flows
through that one app. Keep `/healthz` exempt for the probes.
