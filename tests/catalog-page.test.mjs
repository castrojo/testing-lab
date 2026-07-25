import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

const repo = process.cwd();

function html(file) {
  return readFileSync(path.join(repo, file), 'utf8');
}

test('catalog page renders provider apps, installed state, search/filter controls, and install commands', () => {
  execFileSync('env', ['-i', `PATH=${process.env.PATH}`, 'npm', 'run', 'build'], {
    cwd: repo,
    stdio: 'pipe',
    encoding: 'utf8',
  });

  const catalogPage = html('docs/catalog/index.html');

  assert.equal(existsSync(path.join(repo, 'docs/catalog/index.html')), true, 'catalog page exists after build');
  assert.match(catalogPage, /App Catalog/i, 'catalog page renders the title');
  assert.match(catalogPage, /Providers/i, 'catalog page renders provider KPI');
  assert.match(catalogPage, /Apps/i, 'catalog page renders app count KPI');
  assert.match(catalogPage, /Installed/i, 'catalog page renders installed count KPI');
  assert.match(catalogPage, /jellyfin/i, 'catalog page renders the installed jellyfin app');
  assert.match(catalogPage, /linuxserver/i, 'catalog page renders provider badge');
  assert.match(catalogPage, /catalog-search/, 'catalog page renders search input');
  assert.match(catalogPage, /catalog-category/, 'catalog page renders category filter');
  assert.match(catalogPage, /catalog-provider/, 'catalog page renders provider filter');
  assert.match(catalogPage, /catalog-installed/, 'catalog page renders installed filter');
  assert.match(catalogPage, /Install/, 'catalog page renders install affordance');
  assert.match(catalogPage, /argo submit --from workflowtemplate\/catalog-install-lsio/, 'catalog page renders install command');
  assert.match(catalogPage, /WORKFLOWS\.html#catalog-install-lsio/, 'catalog page links to install workflow docs');
  assert.match(catalogPage, /catalog-page-data/, 'catalog page serializes client-side payload');
  assert.match(catalogPage, /data-cfasync="false"/, 'catalog page marks runtime script Rocket-Loader-exempt');
});
