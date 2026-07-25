# KubeStellar Console missions

This directory contains custom `kc-mission-v1` guided missions for the
KubeStellar Console.

## Loading mechanism (current gap)

The Console v0.3.34 does **not** read custom missions from an in-cluster
ConfigMap or CRD. It discovers missions from the public
`kubestellar/console-kb` GitHub repository (`fixes/index.json`) and supports
local file import through the Console UI. Therefore the mission files in this
directory are:

- committed to git as the source of truth for the lab,
- imported into the Console via **Missions > Local Files > Import**, and
- candidates for upstream contribution to `kubestellar/console-kb` if they
  should ship by default.

Do not `kubectl apply` these files; they are JSON payloads, not Kubernetes
manifests.

## Missions

- [`add-your-second-pc.json`](add-your-second-pc.json) — onboard a second PC
  as either a k3s agent node in the shared cluster or a new WEC.
