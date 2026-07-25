import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

export interface CatalogApp {
  name: string;
  description: string;
  category: string;
  logo_url: string | null;
  image_ref: string;
  monthly_pulls: number | null;
  stars: number | null;
  architectures: string[];
  config_pointer: string | null;
  readonly_supported: boolean;
  nonroot_supported: boolean;
  verified: boolean;
}

export interface CatalogProvider {
  provider: string;
  generated_at: string;
  source_api: string;
  apps: CatalogApp[];
}

export interface InstalledApp {
  provider: string;
  name: string;
  namespace: string;
  manifest_path: string;
  installed_at: string;
}

export interface CatalogInstallList {
  schema_version: string;
  _meta: {
    generated_at: string;
    description: string;
    status: string;
  };
  installed: InstalledApp[];
}

export interface EnrichedApp extends CatalogApp {
  provider: string;
  installed: boolean;
  namespace: string | null;
  manifest_path: string | null;
  installed_at: string | null;
}

export interface CatalogPageModel {
  providers: CatalogProvider[];
  installed: InstalledApp[];
  apps: EnrichedApp[];
  summary: {
    provider_count: number;
    app_count: number;
    installed_count: number;
    category_count: number;
    generated_at: string;
    status: string;
  };
  state: 'available' | 'unavailable';
  state_reason: string | null;
}

function readJson<T>(path: string, fallback: T): T {
  if (!existsSync(path)) {
    return fallback;
  }
  try {
    return JSON.parse(readFileSync(path, 'utf8')) as T;
  } catch {
    return fallback;
  }
}

function isValidProvider(obj: any): obj is CatalogProvider {
  return (
    obj &&
    typeof obj.provider === 'string' &&
    typeof obj.source_api === 'string' &&
    Array.isArray(obj.apps)
  );
}

export function loadCatalogPageModel(repoRoot: string): CatalogPageModel {
  const catalogDir = join(repoRoot, 'docs/data/catalog');
  const installedPath = join(catalogDir, 'installed.json');

  const installedData = readJson<CatalogInstallList>(installedPath, {
    schema_version: '1.0',
    _meta: { generated_at: '', description: '', status: 'unavailable' },
    installed: []
  });

  const installedByKey = new Map<string, InstalledApp>();
  for (const item of installedData.installed || []) {
    if (item.provider && item.name) {
      installedByKey.set(`${item.provider}:${item.name}`, item);
    }
  }

  let providerFiles: string[] = [];
  if (existsSync(catalogDir)) {
    try {
      providerFiles = readdirSync(catalogDir)
        .filter((f) => f.endsWith('.json') && f !== 'installed.json');
    } catch {
      providerFiles = [];
    }
  }

  const providers: CatalogProvider[] = [];
  let latestGeneratedAt = '';
  for (const file of providerFiles) {
    const raw = readJson<any>(join(catalogDir, file), null);
    if (!isValidProvider(raw)) {
      continue;
    }
    providers.push(raw);
    if (raw.generated_at && raw.generated_at > latestGeneratedAt) {
      latestGeneratedAt = raw.generated_at;
    }
  }

  const apps: EnrichedApp[] = [];
  const categories = new Set<string>();
  for (const provider of providers) {
    for (const app of provider.apps || []) {
      const key = `${provider.provider}:${app.name}`;
      const installRecord = installedByKey.get(key);
      const categoryList = (app.category || 'Uncategorized')
        .split(/[,;]/)
        .map((c) => c.trim())
        .filter(Boolean);
      for (const c of categoryList) {
        categories.add(c);
      }
      apps.push({
        ...app,
        provider: provider.provider,
        installed: !!installRecord,
        namespace: installRecord?.namespace ?? null,
        manifest_path: installRecord?.manifest_path ?? null,
        installed_at: installRecord?.installed_at ?? null
      });
    }
  }

  const state: 'available' | 'unavailable' = providers.length > 0 ? 'available' : 'unavailable';
  const state_reason = state === 'unavailable'
    ? 'No provider indexes found in docs/data/catalog/. Run the catalog poller or add a provider index matching the schema.'
    : null;

  return {
    providers,
    installed: installedData.installed || [],
    apps,
    summary: {
      provider_count: providers.length,
      app_count: apps.length,
      installed_count: installedData.installed?.length ?? 0,
      category_count: categories.size,
      generated_at: latestGeneratedAt || installedData._meta?.generated_at || new Date().toISOString(),
      status: state === 'available' ? 'ok' : 'unavailable'
    },
    state,
    state_reason
  };
}
