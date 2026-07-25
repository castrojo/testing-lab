# Catalog index schema

Provider index files live at `docs/data/catalog/<provider>.json`. They are
thin, refreshable metadata: the index describes what an upstream provider
publishes and where to fetch runtime configuration at deploy time. It does
**not** contain Kubernetes manifests, vendored compose files, or deployment
logic.

## File format

```json
{
  "provider": "linuxserver",
  "generated_at": "2026-07-24T23:15:00Z",
  "source_api": "https://api.linuxserver.io/api/v1/images?include_config=true",
  "apps": [
    {
      "name": "adguardhome-sync",
      "description": "Synchronize AdGuardHome config to replica instances.",
      "category": "Network,DNS",
      "logo_url": "https://raw.githubusercontent.com/linuxserver/docker-templates/...",
      "image_ref": "lscr.io/linuxserver/adguardhome-sync",
      "monthly_pulls": 100951,
      "stars": 65,
      "architectures": ["x86_64", "arm64"],
      "config_pointer": "https://github.com/linuxserver/docker-adguardhome-sync?tab=readme-ov-file#application-setup",
      "readonly_supported": true,
      "nonroot_supported": true,
      "verified": false
    }
  ]
}
```

## Fields

Top level:

| Field | Type | Description |
|-------|------|-------------|
| `provider` | string | Short provider identifier, matches the file base name. |
| `generated_at` | string | ISO-8601 UTC timestamp when the index was generated. |
| `source_api` | string | Upstream API endpoint that produced the index. |
| `apps` | array | Provider applications, sorted deterministically by `name`. |

Per-app object:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Canonical application name. |
| `description` | string | Short human-readable description. |
| `category` | string | Provider category label; may be a comma-separated list. |
| `logo_url` | string or null | URL to a provider-hosted logo or icon. |
| `image_ref` | string | Canonical OCI image reference, without tag. |
| `monthly_pulls` | integer or null | Estimated monthly pull count, if published. |
| `stars` | integer or null | Upstream star/favorite count, if published. |
| `architectures` | array of strings | Supported CPU architectures. |
| `config_pointer` | string or null | Upstream URL to fetch configuration from at deploy time. For linuxserver.io this is the `application_setup` README anchor. |
| `readonly_supported` | boolean | Whether the image supports a read-only rootfs. |
| `nonroot_supported` | boolean | Whether the image supports running as a non-root user. |
| `verified` | boolean | Provider verification/trusted badge. Defaults to `false` when the provider does not expose this concept. |

## Provider tiers

The schema is provider-agnostic, but not every provider exposes the same
richness:

* **linuxserver.io** is the reference rich-API tier. All fields above are
  populated from `https://api.linuxserver.io/api/v1/images?include_config=true`.
* Future providers such as NVIDIA NGC, AMD, or DockerHub may leave many fields
  `null` or `false` and only populate `name`, `description`, `image_ref`, and
  `config_pointer`.

## Refresh

The `linuxserver.json` index is refreshed weekly by the
`catalog-lsio-poller` CronWorkflow in `manifests/catalog-lsio-poller.yaml`.
The poller opens a pull request when the upstream catalog changes.
