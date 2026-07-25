#!/usr/bin/env python3
"""Refresh the NVIDIA NGC catalog index.

Queries the public NGC catalog search API for official NVIDIA containers and
maps the response to the provider-agnostic catalog index schema defined in
docs/reference/catalog-index-schema.md. The resulting thin index is written to
docs/data/catalog/ngc.json sorted by container name.

The public search endpoint does not require authentication. Images are pulled
from nvcr.io separately and do require auth.

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
import urllib.parse
from pathlib import Path

# Public NGC catalog search endpoint for containers.
API_URL = "https://api.ngc.nvidia.com/v2/search/catalog/resources/CONTAINER"
OUT_PATH = Path("docs/data/catalog/ngc.json")
PROVIDER = "ngc"
IMAGE_PREFIX = "nvcr.io"
PAGE_SIZE = 100
MAX_PAGES = 200


class CatalogError(Exception):
    pass


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_page(page=0, page_size=PAGE_SIZE, timeout=60):
    query = {"query": "*", "pageSize": page_size, "page": page}
    url = f"{API_URL}?q={urllib.parse.quote(json.dumps(query))}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "bluefin-lab-catalog-poller/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def is_nvidia_container(container):
    """Return True for official NVIDIA-published containers."""
    return container.get("orgName") == "nvidia"


def fetch_all_containers():
    """Fetch all public NVIDIA container listings from the NGC catalog API.

    Filters to orgName == "nvidia" to keep the index limited to official
    NVIDIA-published containers and exclude test/user organizations.
    """
    containers = []
    seen = set()
    for page in range(MAX_PAGES):
        data = fetch_page(page=page)
        for group in data.get("results") or []:
            for resource in group.get("resources") or []:
                if not is_nvidia_container(resource):
                    continue
                rid = resource.get("resourceId")
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                containers.append(resource)

        result_total = data.get("resultTotal")
        result_page_total = data.get("resultPageTotal")
        if result_page_total is not None and page + 1 >= result_page_total:
            break
        if result_total is not None and len(containers) >= result_total:
            break
    return containers


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_name(container):
    """Return the canonical container name from resourceId or display fields."""
    name = container.get("name")
    if name and isinstance(name, str):
        return name.strip()
    rid = container.get("resourceId") or ""
    if "/" in rid:
        return rid.split("/")[-1].strip()
    return rid.strip() or None


def get_display_name(container):
    display = container.get("displayName")
    if display and isinstance(display, str):
        return display.strip()
    return get_name(container)


def get_description(container):
    description = container.get("description")
    if description and isinstance(description, str):
        return description.strip()
    return None


def get_attributes(container):
    """Return attributes as a dict keyed by attribute key."""
    attrs = container.get("attributes") or []
    return {a.get("key"): a.get("value") for a in attrs if a.get("key")}


def get_category(container):
    """Build a category string from the 'general' label group."""
    labels = container.get("labels") or []
    for label in labels:
        if label.get("key") == "general":
            values = label.get("values") or []
            # Prefer human-readable unresolved values when available.
            if not values:
                values = label.get("unresolvedValues") or []
            return ", ".join(str(v) for v in values if v) or "AI/ML"
    return "AI/ML"


def get_logo_url(container):
    attrs = get_attributes(container)
    logo = attrs.get("logo")
    if logo and isinstance(logo, str):
        return logo.strip()
    return None


def get_image_ref(container):
    rid = container.get("resourceId")
    if rid and isinstance(rid, str):
        return f"{IMAGE_PREFIX}/{rid.strip()}"
    return None


def get_monthly_pulls(container):
    # NGC exposes a popularity score, not a raw pull count.
    weight = container.get("weightPopular") or container.get("weight_popular")
    if weight is not None:
        return as_int(weight)
    return None


def get_stars(container):
    return None


def get_architectures(container):
    """Derive architectures from system labels; default to x86_64 for NVIDIA containers."""
    labels = container.get("labels") or []
    system_values = []
    for label in labels:
        if label.get("key") == "system":
            system_values = label.get("values") or label.get("unresolvedValues") or []
            break
    has_multiarch = any("multiarch" in str(v).lower() for v in system_values)
    if has_multiarch:
        return ["x86_64", "arm64"]
    # NVIDIA containers are x86_64 unless explicitly marked multiarch.
    return ["x86_64"]


def config_pointer(container):
    """Return the upstream URL consumers should fetch for deploy-time config."""
    rid = container.get("resourceId")
    if rid and isinstance(rid, str):
        return f"https://catalog.ngc.nvidia.com/orgs/{rid.split('/')[0]}/containers/{rid.split('/')[-1]}"
    return None


def map_container(container):
    name = get_name(container)
    image_ref = get_image_ref(container)
    return {
        "name": name,
        "description": get_description(container),
        "category": get_category(container),
        "logo_url": get_logo_url(container),
        "image_ref": image_ref,
        "monthly_pulls": get_monthly_pulls(container),
        "stars": get_stars(container),
        "architectures": get_architectures(container),
        "config_pointer": config_pointer(container),
        "readonly_supported": False,
        "nonroot_supported": False,
        "verified": True,
    }


def build_index(containers):
    apps = []
    seen = set()
    for container in containers:
        mapped = map_container(container)
        name = mapped["name"]
        image_ref = mapped["image_ref"]
        if not name or not image_ref:
            continue
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
