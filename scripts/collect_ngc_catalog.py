#!/usr/bin/env python3
"""Refresh the NVIDIA NGC catalog index.

Queries the public NGC catalog API for NVIDIA-published containers and maps
the response to the provider-agnostic catalog index schema defined in
docs/reference/catalog-index-schema.md. The resulting thin index is written to
docs/data/catalog/ngc.json sorted by container name.

NGC catalog listings are public; pulling images from nvcr.io requires auth,
but this poller only reads metadata.

Run locally:
    python3 scripts/collect_ngc_catalog.py

Dry-run (no files written):
    python3 scripts/collect_ngc_catalog.py --dry-run
"""

import argparse
import datetime
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# NGC catalog API endpoint for NVIDIA containers.
# This endpoint requires an NGC API key for authentication; public
# unauthenticated access is not available for container listings.
API_URL = "https://api.ngc.nvidia.com/v2/orgs/nvidia/containers"
OUT_PATH = Path("docs/data/catalog/ngc.json")
PROVIDER = "ngc"
IMAGE_PREFIX = "nvcr.io/nvidia"
PAGE_SIZE = 100


class CatalogError(Exception):
    pass


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_json(url, timeout=60):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "bluefin-lab-catalog-poller/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_page(page=0, page_size=PAGE_SIZE):
    url = f"{API_URL}?page={page}&pageSize={page_size}"
    return fetch_json(url)


def fetch_all_containers(max_pages=50):
    """Fetch all public NVIDIA container listings from the NGC catalog API."""
    containers = []
    for page in range(max_pages):
        data = fetch_page(page=page)
        page_containers = data.get("containers") or data.get("results") or []
        if not page_containers:
            break
        containers.extend(page_containers)
        total = data.get("total") or data.get("totalCount") or data.get("total_count")
        if total is not None and len(containers) >= total:
            break
        # If the page is not full, we have reached the end.
        if len(page_containers) < PAGE_SIZE:
            break
    return containers


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_name(container):
    """Return the canonical container name from various possible fields."""
    for key in ("name", "containerName", "displayName", "display_name"):
        value = container.get(key)
        if value and isinstance(value, str):
            return value.strip()
    return None


def get_description(container):
    for key in ("description", "shortDescription", "short_description", "summary"):
        value = container.get(key)
        if value and isinstance(value, str):
            return value.strip()
    return None


def get_category(container):
    value = container.get("labels") or container.get("tags") or []
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v) or "AI/ML"
    if isinstance(value, dict):
        return ", ".join(str(v) for v in value.values() if v) or "AI/ML"
    if isinstance(value, str):
        return value
    return "AI/ML"


def get_logo_url(container):
    for key in ("logoUrl", "logo_url", "logo", "iconUrl", "icon_url"):
        value = container.get(key)
        if value and isinstance(value, str):
            return value.strip()
    return None


def get_image_ref(container, name):
    for key in ("imagePath", "image_path", "imageUrl", "image_url", "repository"):
        value = container.get(key)
        if value and isinstance(value, str):
            return value.split(":")[0].strip()
    if name:
        return f"{IMAGE_PREFIX}/{name}"
    return None


def get_pulls(container):
    for key in ("downloads", "pullCount", "pull_count", "monthlyPulls", "monthly_pulls"):
        value = container.get(key)
        if value is not None:
            return as_int(value)
    return None


def get_stars(container):
    for key in ("likes", "favorites", "stars"):
        value = container.get(key)
        if value is not None:
            return as_int(value)
    return None


def get_architectures(container):
    arches = container.get("architectures") or container.get("architecture") or []
    if isinstance(arches, str):
        arches = [a.strip() for a in arches.split(",")]
    names = []
    for a in arches or []:
        if isinstance(a, dict):
            name = a.get("arch") or a.get("architecture") or a.get("name")
        else:
            name = a
        if name and isinstance(name, str):
            names.append(name.strip())
    # Normalize common forms to the schema's vocabulary.
    normalized = []
    mapping = {"amd64": "x86_64", "x64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}
    seen = set()
    for name in names:
        canonical = mapping.get(name.lower(), name)
        if canonical not in seen:
            seen.add(canonical)
            normalized.append(canonical)
    return normalized


def config_pointer(container, name):
    """Return the upstream URL consumers should fetch for deploy-time config."""
    for key in ("docsUrl", "docs_url", "documentationUrl", "documentation_url"):
        value = container.get(key)
        if value and isinstance(value, str):
            return value.strip()
    if name:
        return f"https://catalog.ngc.nvidia.com/orgs/nvidia/containers/{name}"
    return None


def is_container(container):
    """Return True if the catalog entry is a container (not a model or chart)."""
    resource_type = container.get("resourceType") or container.get("resource_type") or ""
    if resource_type and resource_type.lower() != "container":
        return False
    # Accept entries that explicitly carry an image path/repository.
    if container.get("imagePath") or container.get("image_path") or container.get("repository"):
        return True
    # Fallback: accept when no resourceType is present (assume container listing).
    return True


def map_container(container):
    name = get_name(container)
    image_ref = get_image_ref(container, name)
    return {
        "name": name,
        "description": get_description(container),
        "category": get_category(container),
        "logo_url": get_logo_url(container),
        "image_ref": image_ref,
        "monthly_pulls": get_pulls(container),
        "stars": get_stars(container),
        "architectures": get_architectures(container),
        "config_pointer": config_pointer(container, name),
        "readonly_supported": False,
        "nonroot_supported": False,
        "verified": True,
    }


def build_index(containers):
    apps = []
    seen = set()
    for container in containers:
        if not is_container(container):
            continue
        mapped = map_container(container)
        name = mapped["name"]
        if not name or not mapped["image_ref"]:
            continue
        # Deduplicate by name.
        if name in seen:
            continue
        seen.add(name)
        apps.append(mapped)
    apps.sort(key=lambda a: a["name"])
    return {
        "provider": PROVIDER,
        "generated_at": now_iso(),
        "source_api": API_URL,
        "apps": apps,
    }


def main():
    parser = argparse.ArgumentParser(description="Refresh the NVIDIA NGC catalog index.")
    parser.add_argument("--dry-run", action="store_true", help="Print summary but do not write the index file.")
    args = parser.parse_args()

    try:
        containers = fetch_all_containers()
    except urllib.error.HTTPError as exc:
        print(f"ERROR: HTTP {exc.code} from {API_URL}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"ERROR: failed to reach {API_URL}: {exc.reason}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON from {API_URL}: {exc}", file=sys.stderr)
        return 1
    except CatalogError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    index = build_index(containers)
    apps = index["apps"]

    if args.dry_run:
        print(f"provider: {index['provider']}")
        print(f"generated_at: {index['generated_at']}")
        print(f"source_api: {index['source_api']}")
        print(f"apps: {len(apps)}")
        return 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")
    print(f"catalog-ngc: wrote {len(apps)} apps to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
