// Stage a baked Semantic CDN tree into the literal URL paths a *static* host
// (IPFS/Pinata, Netlify, plain nginx) serves — no `quickbeam cdn serve` in the
// loop. `serve` rewrites request paths in code; a static host won't, so we
// materialize the rewrite as files:
//
//   serve route                       static file under <dest>
//   ------------------------------     ----------------------------------------
//   GET /catalog                   ->  catalog
//   GET /domains/<d>/manifest      ->  domains/<d>/manifest
//   GET /domains/<d>/shards/<f>    ->  domains/<d>/shards/<f>
//
// The client (lib/shards.ts) fetches those exact paths with VITE_CDN_URL=/cdn,
// gunzips shards itself (DecompressionStream) and parses manifest/catalog with
// res.json() — which ignores Content-Type — so extensionless files are fine.
//
// Usage:
//   node scripts/stage-cdn.mjs [--src ../cdn] [--dest public/cdn] [--domain places ...]
// With no --domain, every domain present on disk is staged. --domain limits the
// deploy (and rewrites the catalog to just those) so a single-domain site like
// places.sond3r.com doesn't ship unrelated corpora.

import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync, copyFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');

function parseArgs(argv) {
  const out = { src: resolve(root, '../cdn'), dest: resolve(root, 'public/cdn'), domains: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--src') out.src = resolve(argv[++i]);
    else if (a === '--dest') out.dest = resolve(argv[++i]);
    else if (a === '--domain') out.domains.push(argv[++i]);
    else throw new Error(`unknown arg: ${a}`);
  }
  return out;
}

function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

const { src, dest, domains: wanted } = parseArgs(process.argv.slice(2));

const catalogPath = join(src, 'catalog.json');
if (!existsSync(catalogPath)) {
  console.error(`[stage-cdn] no catalog at ${catalogPath} — run \`quickbeam cdn bake --cdn-dir ${src}\` first`);
  process.exit(1);
}

const catalog = JSON.parse(readFileSync(catalogPath, 'utf8'));
const allDomains = Array.isArray(catalog.domains) ? catalog.domains : [];

// Pick the domains to stage: requested set if given, else everything that has a
// real directory (the catalog can list stale entries whose dir was deleted).
const onDisk = (name) => existsSync(join(src, name)) && statSync(join(src, name)).isDirectory();
let chosen = (wanted.length ? allDomains.filter((d) => wanted.includes(d.name)) : allDomains)
  .filter((d) => onDisk(d.name));

if (wanted.length) {
  const missing = wanted.filter((w) => !chosen.some((d) => d.name === w));
  if (missing.length) {
    console.error(`[stage-cdn] requested domain(s) not baked on disk: ${missing.join(', ')}`);
    process.exit(1);
  }
}
if (chosen.length === 0) {
  console.error('[stage-cdn] no stageable domains found');
  process.exit(1);
}

// Clean dest so a removed shard never lingers as a stale immutable file.
rmSync(dest, { recursive: true, force: true });
mkdirSync(dest, { recursive: true });

// Filtered catalog (only staged domains) — written extensionless to match /catalog.
const stagedCatalog = { ...catalog, domains: chosen };
writeFileSync(join(dest, 'catalog'), JSON.stringify(stagedCatalog));

let totalFiles = 1;
let totalBytes = Buffer.byteLength(JSON.stringify(stagedCatalog));

for (const d of chosen) {
  const srcDir = join(src, d.name);
  const domDest = join(dest, 'domains', d.name);
  const shardsDest = join(domDest, 'shards');
  mkdirSync(shardsDest, { recursive: true });

  // manifest.json -> domains/<d>/manifest
  const manifestSrc = join(srcDir, 'manifest.json');
  if (!existsSync(manifestSrc)) {
    console.error(`[stage-cdn] ${d.name}: missing manifest.json`);
    process.exit(1);
  }
  copyFileSync(manifestSrc, join(domDest, 'manifest'));
  totalFiles++;
  totalBytes += statSync(manifestSrc).size;

  // shard-*.ndjson.gz -> domains/<d>/shards/<file> (names carried verbatim; the
  // manifest references them by name, so they must not be renamed).
  const shards = readdirSync(srcDir).filter((f) => f.startsWith('shard-') && f.endsWith('.ndjson.gz'));
  if (shards.length === 0) {
    console.error(`[stage-cdn] ${d.name}: no shard files`);
    process.exit(1);
  }
  for (const f of shards) {
    copyFileSync(join(srcDir, f), join(shardsDest, f));
    totalFiles++;
    totalBytes += statSync(join(srcDir, f)).size;
  }
  console.log(`[stage-cdn] ${d.name}: manifest + ${shards.length} shard(s)`);
}

console.log(`[stage-cdn] staged ${chosen.length} domain(s), ${totalFiles} files, ${fmtBytes(totalBytes)} -> ${dest}`);
