# ADR 0004 — Network stack: no Cilium, Gateway API on Traefik

Status: Accepted
Date: 2026-07-24

## Context

ingress-nginx was retired upstream (EOL March 2026, no security fixes;
Gateway API is the official successor). KubeFlex's documented external
reachability path hardcodes `ingressClassName: nginx` +
`nginx.ingress.kubernetes.io/ssl-passthrough` and has no Gateway API
support. A full network-stack review evaluated standardizing on Cilium
(CNI + kube-proxy replacement + LB-IPAM + Gateway API + Hubble).

## Decision

**Stay on flannel (host-gw) + Traefik. Adopt Gateway API at the ingress
layer when external reachability is needed. ingress-nginx is banned.
Cilium is rejected.**

### Why Cilium is rejected

1. **USB4 showstopper**: Cilium's eBPF host routing bypasses Linux policy
   routing (`ip rule`) entirely. The lab steers pod-to-pod build traffic
   over the 40 Gbps point-to-point USB4 link via policy routing (table 40);
   Cilium would silently move BuildBarn/BuildStream traffic to 1 Gbps
   Ethernet — a 40x regression on the platform's signature feature.
2. No IP-preserving KubeVirt live migration on Cilium's default pod network
   — breaks the shared-cluster migration story.
3. ~0.5–1.5 GB RAM per node for agent + Envoy + operator vs ~110 MB for
   flannel + Traefik; unacceptable tax on N100-class nodes.
4. In-place CNI migration on a live cluster with KubeVirt VMs is
   effectively a rebuild.

### Forward path (when external control-plane reachability is needed)

Phase 1 needs none: in the shared cluster, OCM agents and controllers reach
KubeFlex hosted control planes via in-cluster Service DNS. When external
WECs register:

1. Install Gateway API Standard channel CRDs (v1.6.1+; `TLSRoute` is GA).
2. Enable Traefik's native provider via HelmChartConfig:
   `providers.kubernetesGateway.enabled: true`.
3. Declare `Gateway` + `TLSRoute` (mode: Passthrough) in git per hosted
   control plane. KubeFlex's hardcoded nginx `Ingress` objects sit ignored
   — no controller claims them; zero upstream changes needed.
4. Fallback: `LoadBalancer` Service per control plane via k3s ServiceLB
   (dedicated LAN IP, no SNI routing).

## Consequences

- No new networking controllers today (ADR-0001 minimalism).
- KubeFlex-generated Ingress resources are expected inert clutter; do not
  install any controller that would claim `ingressClassName: nginx`.
- Tailscale remains the cross-site transport for the BST CAS grid.
