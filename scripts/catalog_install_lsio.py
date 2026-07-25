#!/usr/bin/env python3
"""Deploy-time translator from linuxserver.io catalog config to Kubernetes manifests.

Reads the app entry from the linuxserver.io images API (or a local catalog
index), applies lab conventions, and writes rendered manifests to a directory.

Lab conventions applied:
  * Storage class is local-path; PVCs are created on node data disks.
  * Image references use the bare docker.io form (linuxserver/<app>:latest)
    so the cluster's zot-docker mirror resolves them.
  * PUID/PGID become both env vars and a securityContext (fsGroup/runAsGroup;
    runAsUser/runAsNonRoot only when the image advertises non-root support).
  * Optional ports and volumes are included by default; the installer favours
    a working out-of-the-box config over minimalism.
  * External ingress is deferred to Gateway API (ADR-0004); a ClusterIP
    Service is emitted for in-cluster reachability.

Usage:
    python3 scripts/catalog_install_lsio.py jellyfin
    python3 scripts/catalog_install_lsio.py jellyfin --mode imperative --namespace media
    python3 scripts/catalog_install_lsio.py sonarr --catalog-path docs/data/catalog/linuxserver.json
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

API_URL = "https://api.linuxserver.io/api/v1/images?include_config=true"
DEFAULT_STORAGE_CLASS = "local-path"
IMAGE_PREFIX = "linuxserver"

# Path-based heuristic for PVC size. These are starting sizes; operators can
# expand the PVCs after install.
SIZE_HINTS = [
    ("/transcode", "50Gi"),
    ("/config", "5Gi"),
    ("media", "100Gi"),
    ("movies", "100Gi"),
    ("tv", "100Gi"),
    ("downloads", "100Gi"),
    ("data", "100Gi"),
]
DEFAULT_SIZE = "1Gi"


def load_catalog(path=None):
    """Load catalog JSON from a local file or the LSIO API."""
    if path:
        with open(path) as f:
            return json.load(f)
    req = urllib.request.Request(API_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def find_app(catalog, name):
    """Return the catalog entry for ``name`` or raise ValueError.

    Accepts both the upstream API shape (data.repositories.linuxserver)
    and the normalized index shape (apps).
    """
    repositories = (catalog.get("data") or {}).get("repositories") or {}
    images = repositories.get("linuxserver") or []
    if not images and "apps" in catalog:
        images = catalog["apps"]
    for image in images:
        if image.get("name") == name:
            return image
    raise ValueError(f"app '{name}' not found in linuxserver.io catalog")


def pvc_name(app_name, mount_path):
    """Derive a stable PVC name from the app name and mount path."""
    suffix = mount_path.strip("/").replace("/", "-")
    suffix = re.sub(r"[^a-z0-9-]+", "-", suffix.lower()).strip("-")
    return f"{app_name}-{suffix}" if suffix else app_name


def pvc_size(mount_path):
    """Return a heuristic PVC size for ``mount_path``."""
    lowered = mount_path.lower()
    for hint, size in SIZE_HINTS:
        if hint in lowered:
            return size
    return DEFAULT_SIZE


def parse_port(port_value):
    """Parse an LSIO port value like '8096', '8096/tcp', or '7359/udp'."""
    text = str(port_value)
    if "/" in text:
        num, proto = text.split("/", 1)
    else:
        num, proto = text, "tcp"
    try:
        return int(num), proto.lower()
    except ValueError:
        return None, None


def env_vars_from_config(config):
    """Render k8s env list from LSIO config.env_vars."""
    result = []
    for ev in config.get("env_vars") or []:
        name = ev.get("name")
        if not name:
            continue
        entry = {"name": name, "value": str(ev.get("value", ""))}
        result.append(entry)
    return result


def security_context_from_config(config):
    """Build a pod-level securityContext from PUID/PGID/flags."""
    env_map = {ev.get("name"): ev.get("value") for ev in config.get("env_vars") or []}
    puid = env_map.get("PUID")
    pgid = env_map.get("PGID")
    nonroot = config.get("nonroot_supported", False)
    readonly = config.get("readonly_supported", False)

    ctx = {}
    if pgid is not None:
        try:
            ctx["fsGroup"] = int(pgid)
        except ValueError:
            pass
    if nonroot:
        if puid is not None:
            try:
                ctx["runAsUser"] = int(puid)
            except ValueError:
                pass
        if pgid is not None:
            try:
                ctx["runAsGroup"] = int(pgid)
            except ValueError:
                pass
        ctx["runAsNonRoot"] = True
    if readonly:
        ctx["readOnlyRootFilesystem"] = True
    return ctx if ctx else None


<<<<<<< HEAD
def render_volumes(app_name, volumes, namespace):
=======
def render_volumes(app_name, volumes):
>>>>>>> origin/main
    """Return (pvcs, volume_mounts, volumes) for the Deployment."""
    pvcs = []
    mounts = []
    vols = []
    seen = set()
    for vol in volumes or []:
        path = vol.get("path")
        if not path or path in seen:
            continue
        seen.add(path)
        name = pvc_name(app_name, path)
        pvcs.append({
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": name,
<<<<<<< HEAD
                "namespace": namespace,
=======
>>>>>>> origin/main
                "labels": {
                    "app": app_name,
                    "app.kubernetes.io/part-of": "catalog-apps",
                    "bluefin.io/catalog-provider": "linuxserver",
                },
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": DEFAULT_STORAGE_CLASS,
                "resources": {"requests": {"storage": pvc_size(path)}},
            },
        })
        mounts.append({"name": name, "mountPath": path})
        vols.append({"name": name, "persistentVolumeClaim": {"claimName": name}})
    return pvcs, mounts, vols


def render_ports(ports):
    """Return (container_ports, service_ports) from LSIO config.ports."""
    container_ports = []
    service_ports = []
    seen = set()
    for p in ports or []:
        internal = p.get("internal") or p.get("external")
        external = p.get("external") or p.get("internal")
        port_num, proto = parse_port(internal)
        ext_num, _ = parse_port(external)
        if port_num is None:
            continue
        key = (port_num, proto)
        if key in seen:
            continue
        seen.add(key)
        name = f"{proto}-{port_num}"
        container_ports.append({
            "name": name,
            "containerPort": port_num,
            "protocol": proto.upper(),
        })
        service_ports.append({
            "name": name,
            "port": ext_num if ext_num is not None else port_num,
            "targetPort": port_num,
            "protocol": proto.upper(),
        })
    return container_ports, service_ports


def render_manifests(app_name, entry, namespace, image_tag="latest"):
    """Render the full manifest resource list for ``app_name``."""
    config = entry.get("config") or {}
    envs = env_vars_from_config(config)
<<<<<<< HEAD
    pvcs, mounts, vols = render_volumes(app_name, config.get("volumes"), namespace)
=======
    pvcs, mounts, vols = render_volumes(app_name, config.get("volumes"))
>>>>>>> origin/main
    container_ports, service_ports = render_ports(config.get("ports"))
    sec_ctx = security_context_from_config(config)

    # Use the bare docker.io form so the zot-docker mirror resolves it.
    image = f"{IMAGE_PREFIX}/{app_name}:{image_tag}"

    namespace_res = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "labels": {
                "app.kubernetes.io/part-of": "catalog-apps",
                "bluefin.io/catalog-provider": "linuxserver",
            },
        },
    }

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": app_name,
            "namespace": namespace,
            "labels": {
                "app": app_name,
                "app.kubernetes.io/part-of": "catalog-apps",
                "bluefin.io/catalog-provider": "linuxserver",
            },
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": app_name}},
            "template": {
                "metadata": {
                    "labels": {
                        "app": app_name,
                        "app.kubernetes.io/part-of": "catalog-apps",
                        "bluefin.io/catalog-provider": "linuxserver",
                    },
                },
                "spec": {
                    "containers": [{
                        "name": app_name,
                        "image": image,
                        "ports": container_ports,
                        "env": envs,
                        "volumeMounts": mounts,
                        "resources": {
                            "requests": {
                                "cpu": "100m",
                                "memory": "128Mi",
                                "ephemeral-storage": "100Mi",
                            },
                            "limits": {
                                "cpu": "1000m",
                                "memory": "1Gi",
                                "ephemeral-storage": "1Gi",
                            },
                        },
                    }],
                    "volumes": vols,
                },
            },
        },
    }
    if sec_ctx:
        deployment["spec"]["template"]["spec"]["securityContext"] = sec_ctx

    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": app_name,
            "namespace": namespace,
            "labels": {
                "app": app_name,
                "app.kubernetes.io/part-of": "catalog-apps",
                "bluefin.io/catalog-provider": "linuxserver",
            },
        },
        "spec": {
            "selector": {"app": app_name},
            "type": "ClusterIP",
            "ports": service_ports,
        },
    }

    return [namespace_res] + pvcs + [deployment, service]


# ── Minimal deterministic YAML emitter (stdlib only) ─────────────────────────


def _yaml_scalar(value):
    """Return a YAML scalar representation of ``value``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    # Quote strings that look like they could be misinterpreted.
    if text in ("true", "false", "null", "yes", "no", "on", "off"):
        return f'"{text}"'
    if re.match(r"^[-]?\d+(\.\d+)?$", text):
        return f'"{text}"'
    if (":" in text or text.startswith("{") or text.startswith("[") or
            text.startswith("-") or text.startswith("?") or text.startswith("&") or
            "#" in text or text.strip() != text or "\n" in text or
            text in ("", "~") or text.lower() in ("null", "~")):
        return json.dumps(text)
    return text


def _yaml_dump_resource(resource, indent=0):
    """Dump a single resource dict/list to YAML lines."""
    lines = []
    prefix = "  " * indent
    if isinstance(resource, dict):
        for key, value in resource.items():
            if value is None:
                lines.append(f"{prefix}{key}: null")
            elif isinstance(value, (dict, list)):
                if value:
                    lines.append(f"{prefix}{key}:")
                    lines.extend(_yaml_dump_resource(value, indent + 1))
                else:
                    lines.append(f"{prefix}{key}: []" if isinstance(value, list) else f"{prefix}{key}: {{}}")
            else:
                lines.append(f"{prefix}{key}: {_yaml_scalar(value)}")
    elif isinstance(resource, list):
        for item in resource:
            if isinstance(item, dict):
                if not item:
                    lines.append(f"{prefix}- {{}}")
                else:
                    first = True
                    for key, value in item.items():
                        if first:
                            if isinstance(value, (dict, list)):
                                lines.append(f"{prefix}- {key}:")
                                lines.extend(_yaml_dump_resource(value, indent + 2))
                            else:
                                lines.append(f"{prefix}- {key}: {_yaml_scalar(value)}")
                            first = False
                        else:
                            if isinstance(value, (dict, list)):
                                lines.append(f"{prefix}  {key}:")
                                lines.extend(_yaml_dump_resource(value, indent + 2))
                            else:
                                lines.append(f"{prefix}  {key}: {_yaml_scalar(value)}")
            else:
                lines.append(f"{prefix}- {_yaml_scalar(item)}")
    return lines


def to_yaml(resources):
    """Dump a list of resources as a multi-document YAML string."""
    docs = []
    for resource in resources:
        docs.append("---")
        docs.extend(_yaml_dump_resource(resource))
    if docs:
        docs.append("")
    return "\n".join(docs)


def main():
    parser = argparse.ArgumentParser(description="Render linuxserver.io app manifests for Kubernetes.")
    parser.add_argument("app", help="Application name from the linuxserver.io catalog")
    parser.add_argument("--mode", choices=["gitops", "imperative"], default="gitops",
                        help="Install mode (default: gitops)")
    parser.add_argument("--namespace", default=None, help="Target namespace (default: catalog-<app>)")
    parser.add_argument("--output-dir", default=None,
                        help="Directory to write rendered manifests (default: ./manifests/catalog-apps/<app>)")
    parser.add_argument("--catalog-path", default=None,
                        help="Local catalog JSON file (default: fetch from LSIO API)")
    parser.add_argument("--image-tag", default="latest", help="Image tag (default: latest)")
    parser.add_argument("--storage-class", default=DEFAULT_STORAGE_CLASS,
                        help=f"Storage class for PVCs (default: {DEFAULT_STORAGE_CLASS})")
    args = parser.parse_args()

    namespace = args.namespace or f"catalog-{args.app}"
    output_dir = Path(args.output_dir or f"manifests/catalog-apps/{args.app}")

    catalog = load_catalog(args.catalog_path)
    entry = find_app(catalog, args.app)

    resources = render_manifests(args.app, entry, namespace, args.image_tag)
    yaml_text = to_yaml(resources)

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.yaml"
    manifest_path.write_text(yaml_text)

    print(f"Rendered {len(resources)} resources to {manifest_path}")
    if args.mode == "imperative":
        print("Imperative apply is handled by the caller (kubectl apply -f)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
