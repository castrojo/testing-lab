#!/usr/bin/env python3
"""Refresh the linuxserver.io catalog index.

Fetches https://api.linuxserver.io/api/v1/images?include_config=true,
maps the rich API response to the provider-agnostic catalog index schema
defined in docs/reference/catalog-index-schema.md, and writes
docs/data/catalog/linuxserver.json sorted by application name.

Run locally:
    python3 scripts/collect_lsio_catalog.py

Dry-run (no files written):
    python3 scripts/collect_lsio_catalog.py --dry-run
"""

import argparse
import datetime
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.linuxserver.io/api/v1/images?include_config=true"
OUT_PATH = Path("docs/data/catalog/linuxserver.json")
PROVIDER = "linuxserver"
IMAGE_PREFIX = "lscr.io/linuxserver"


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_architectures(image):
    arches = image.get("architectures") or []
    names = [a.get("arch") for a in arches if a.get("arch")]
    # De-duplicate while preserving order.
    seen = set()
    result = []
    for name in names:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def config_pointer(image):
    """Return the upstream URL consumers should fetch for deploy-time config."""
    config = image.get("config") or {}
    pointer = config.get("application_setup")
    if pointer:
        return pointer
    pointer = image.get("project_url")
    if pointer:
        return pointer
    return image.get("github_url")


def map_image(image):
    config = image.get("config") or {}
    return {
        "name": image.get("name"),
        "description": image.get("description"),
        "category": image.get("category"),
        "logo_url": image.get("project_logo"),
        "image_ref": f"{IMAGE_PREFIX}/{image.get('name')}" if image.get("name") else None,
        "monthly_pulls": as_int(image.get("monthly_pulls")),
        "stars": as_int(image.get("stars")),
        "architectures": extract_architectures(image),
        "config_pointer": config_pointer(image),
        "readonly_supported": bool(config.get("readonly_supported")),
        "nonroot_supported": bool(config.get("nonroot_supported")),
        "verified": False,
    }


def build_index(data):
    repositories = (data.get("data") or {}).get("repositories") or {}
    images = repositories.get("linuxserver") or []
    apps = []
    for image in images:
        if not image.get("name"):
            continue
        apps.append(map_image(image))
    apps.sort(key=lambda a: a["name"])
    return {
        "provider": PROVIDER,
        "generated_at": now_iso(),
        "source_api": API_URL,
        "apps": apps,
    }


def main():
    parser = argparse.ArgumentParser(description="Refresh the linuxserver.io catalog index.")
    parser.add_argument("--dry-run", action="store_true", help="Print summary but do not write the index file.")
    args = parser.parse_args()

    try:
        data = fetch_json(API_URL)
    except urllib.error.HTTPError as exc:
        print(f"ERROR: HTTP {exc.code} from {API_URL}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"ERROR: failed to reach {API_URL}: {exc.reason}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON from {API_URL}: {exc}", file=sys.stderr)
        return 1

    index = build_index(data)
    apps = index["apps"]

    if args.dry_run:
        print(f"provider: {index['provider']}")
        print(f"generated_at: {index['generated_at']}")
        print(f"source_api: {index['source_api']}")
        print(f"apps: {len(apps)}")
        return 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")
    print(f"catalog-lsio: wrote {len(apps)} apps to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
