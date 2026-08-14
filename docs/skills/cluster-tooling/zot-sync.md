# Changing Zot sync prefixes

The `extensions.sync.registries[].content[]` prefix list is not only a
periodic-poll filter. Upstream documents it as: "which content to periodically
pull, **also it's used for filtering ondemand images**"
(source: `/project-zot/zot`, `examples/README.md`). Narrowing that list
therefore changes which images the cluster can pull at all — an omitted prefix
is a failed pull, not a slow one. Treat any prefix edit as a cluster-wide
change, not a tuning tweak.

Before editing, derive the inventory from what the cache has actually served
rather than from the manifest or from memory. Port-forward the cache Service
rather than hardcoding a node address:

```bash
kubectl -n local-registry port-forward svc/zot-cache 15000:5000
curl -s "http://127.0.0.1:15000/metrics" | grep zot_repo_downloads_total
curl -s "http://127.0.0.1:15000/v2/_catalog?n=500"
```

Glob semantics: `*` matches one repository path segment and `**` matches
recursively. Nested repositories such as `kagent-dev/kagent/controller`
require `kagent-dev/**`.

### Verifying the rollout

`skopeo inspect` against the cache is **not** a valid pass/fail signal here,
because the two outcomes look nothing alike:

| Symptom | Meaning |
|---|---|
| Fast `404` / `denied` | Prefix is **not** allowed — a real break |
| Request hangs for minutes | Prefix **is** allowed; Zot is syncing every blob before answering |

A multi-GB bootc image legitimately exceeds a 300s timeout on first read, so a
timeout proves nothing. Read the counters instead: a repository that has a
non-zero `zot_repo_downloads_total` after the config-version rollout is being
served, and an absent error series means nothing is being rejected.

Bump `lab.projectbluefin.io/config-version` in the same commit; Zot reads the
subPath-mounted config only at startup.

### Reaching Zot from a workstation

The cache's NodePort is a **LAN** address. When the workstation is on
Tailscale, `kubectl` works (the kubeconfig points at the Tailscale address)
while that LAN address times out — which reads as "every pull is broken" and
invites a false outage call. Port-forward instead of trusting the node
address:

```bash
kubectl -n local-registry port-forward svc/zot-cache 15000:5000
skopeo inspect --tls-verify=false --no-tags docker://127.0.0.1:15000/<repo>:<tag>
```
