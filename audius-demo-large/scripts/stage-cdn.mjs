// Stage a baked Semantic-CDN tree into the static URL paths the client fetches
// (adapted from examples/scripts/stage-cdn.mjs; this variant also stages the edges
// linkset so the in-browser mesh works):
//
//   GET /catalog                 ->  catalog
//   GET /domains/<d>/manifest    ->  domains/<d>/manifest
//   GET /domains/<d>/shards/<f>  ->  domains/<d>/shards/<f>
//   GET /domains/<d>/edges       ->  domains/<d>/edges        (the linkset)
//
// Usage: node scripts/stage-cdn.mjs [--src ../audius-build/cdn] [--dest public/cdn] [--domain audius ...]
//
// Copied from manual/scripts/stage-cdn.mjs unchanged apart from the default --src,
// so the two stay diffable. Its images-bundle branch is inert here (this graph has
// no baked figures; artwork resolves from CIDs at view time).
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync, copyFileSync, cpSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');

function parseArgs(argv) {
  const out = { src: resolve(root, '../audius-build/cdn'), dest: resolve(root, 'public/cdn'), domains: [] };
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
const onDisk = (name) => existsSync(join(src, name)) && statSync(join(src, name)).isDirectory();
const chosen = (wanted.length ? allDomains.filter((d) => wanted.includes(d.name)) : allDomains).filter((d) => onDisk(d.name));

if (wanted.length) {
  const missing = wanted.filter((w) => !chosen.some((d) => d.name === w));
  if (missing.length) { console.error(`[stage-cdn] not baked on disk: ${missing.join(', ')}`); process.exit(1); }
}
if (chosen.length === 0) { console.error('[stage-cdn] no stageable domains found'); process.exit(1); }

rmSync(dest, { recursive: true, force: true });
mkdirSync(dest, { recursive: true });
writeFileSync(join(dest, 'catalog'), JSON.stringify({ ...catalog, domains: chosen }));

let totalFiles = 1;
let totalBytes = Buffer.byteLength(JSON.stringify({ ...catalog, domains: chosen }));

for (const d of chosen) {
  const srcDir = join(src, d.name);
  const domDest = join(dest, 'domains', d.name);
  const shardsDest = join(domDest, 'shards');
  mkdirSync(shardsDest, { recursive: true });

  const manifestSrc = join(srcDir, 'manifest.json');
  if (!existsSync(manifestSrc)) { console.error(`[stage-cdn] ${d.name}: missing manifest.json`); process.exit(1); }
  copyFileSync(manifestSrc, join(domDest, 'manifest'));
  totalFiles++; totalBytes += statSync(manifestSrc).size;

  // Edges linkset (optional): edges.json -> domains/<d>/edges.
  const edgesSrc = join(srcDir, 'edges.json');
  if (existsSync(edgesSrc)) {
    copyFileSync(edgesSrc, join(domDest, 'edges'));
    totalFiles++; totalBytes += statSync(edgesSrc).size;
  }

  // The gzipped twin (13.1 MB -> 0.75 MB), written by `cdn precompute`. The client
  // asks for this first and falls back to the raw file, so omitting it here would
  // silently cost 17x on the one deployment where bandwidth actually matters.
  const edgesGzSrc = join(srcDir, 'edges.json.gz');
  if (existsSync(edgesGzSrc)) {
    copyFileSync(edgesGzSrc, join(domDest, 'edges.gz'));
    totalFiles++; totalBytes += statSync(edgesGzSrc).size;
  }

  // Codebook artifacts from `cdn index`, mirroring cdn serve's /index/{file} route.
  // Staged even though the client does not read them yet: `cdn serve` already serves
  // them, and a static build that quietly lacks them is the kind of drift that costs
  // an afternoon later.
  const indexSrc = join(srcDir, 'index');
  if (existsSync(indexSrc) && statSync(indexSrc).isDirectory()) {
    const indexDest = join(domDest, 'index');
    mkdirSync(indexDest, { recursive: true });
    for (const f of readdirSync(indexSrc).filter((x) => !x.startsWith('.'))) {
      copyFileSync(join(indexSrc, f), join(indexDest, f));
      totalFiles++; totalBytes += statSync(join(indexSrc, f)).size;
    }
  }

  // Figures (optional): pack images/ into ONE bundle domains/<d>/images.json
  // ({file: base64}). The app downloads it once and serves figures from in-memory
  // blob: URLs, so browsing sections makes no per-image request (the host sees one
  // identical bundle fetch for every visitor — nothing about which sections are read).
  const imagesSrc = join(srcDir, 'images');
  if (existsSync(imagesSrc) && statSync(imagesSrc).isDirectory()) {
    const imgs = readdirSync(imagesSrc).filter((f) => !f.startsWith('.'));
    const bundle = {};
    for (const f of imgs) bundle[f] = readFileSync(join(imagesSrc, f)).toString('base64');
    const out = JSON.stringify(bundle);
    writeFileSync(join(domDest, 'images.json'), out);
    totalFiles++; totalBytes += Buffer.byteLength(out);
    console.log(`[stage-cdn] ${d.name}: bundled ${imgs.length} image(s) -> images.json`);
  }

  const shards = readdirSync(srcDir).filter((f) => f.startsWith('shard-') && f.endsWith('.ndjson.gz'));
  if (shards.length === 0) { console.error(`[stage-cdn] ${d.name}: no shard files`); process.exit(1); }
  for (const f of shards) {
    copyFileSync(join(srcDir, f), join(shardsDest, f));
    totalFiles++; totalBytes += statSync(join(srcDir, f)).size;
  }
  console.log(`[stage-cdn] ${d.name}: manifest + ${existsSync(edgesSrc) ? 'edges + ' : ''}${shards.length} shard(s)`);
}

console.log(`[stage-cdn] staged ${chosen.length} domain(s), ${totalFiles} files, ${fmtBytes(totalBytes)} -> ${dest}`);
