// The graph itself: loading the snapshot, embedding a query, scoring, and walking
// the linkset. Deliberately free of worker globals (`self`, `postMessage`) so the
// same code runs under node — which is how it gets tested without a browser. The
// worker is a thin message shim over this.
// Explicit .ts extension: node's ESM resolver requires it, and this module is
// imported directly by the node-side checks as well as bundled by vite.
import { MATRYOSHKA_DIM, QUERY_PREFIX, matryoshka, topK } from './matryoshka.ts';
import * as store from './store.ts';
import * as route from './route.ts';
import { API_URL, INDEX_DOMAIN, NPROBE } from './config.ts';
import type { Edge, Rec, RelationGroup, Stats } from './types.ts';
import { cosToDistance, toFeatures } from '../kernel/adapt.ts';
import {
  describe, deserialiseState, emptyKernel, onPlay, onSkip,
  queryVector as kernelQuery, reweight, serialiseState,
} from '../kernel/SessionKernel.ts';
import type { Hit, KernelState, KernelStateJSON, WeightedHit } from '../kernel/types.ts';

/** What gets stored between visits. See `kernelExport`. */
export interface KernelPersist {
  state: KernelStateJSON;
  /** Ids already signalled on; kept out of a returning visitor's recommendations. */
  seen: string[];
}

/**
 * Greedy per-artist cap, with backfill.
 *
 * Walks the reweighted list in weight order and admits a record only while its
 * artist is under `per`; everything displaced goes to an overflow list, still in
 * weight order.
 *
 * **Fails open, and that is load-bearing.** If the cap cannot fill `k`, the
 * remainder is topped up from the overflow. The artist publisher in this dataset
 * holds exactly ONE Artist record against 41 tracks, so the owner-filtered rail
 * (`kernelRecommend(6, artistOwner)`) would otherwise render two cards and
 * quietly gut the cross-publisher demonstration. Same instinct as
 * `applySuppressionFilter`, which returns an unfiltered list rather than an
 * empty one.
 *
 * Keyed on `metadata.artistId` rather than a display name: `toFeatures` falls
 * back to the record's own id when a track has no artist, so every record has a
 * non-colliding key and no undefined-guard is needed.
 */
const capPerArtist = (ranked: WeightedHit[], k: number, per: number): WeightedHit[] => {
  const out: WeightedHit[] = [];
  const overflow: WeightedHit[] = [];
  const n = new Map<string, number>();
  for (const h of ranked) {
    const a = h.metadata?.artistId ?? h.id;
    const c = n.get(a) ?? 0;
    if (out.length < k && c < per) { n.set(a, c + 1); out.push(h); }
    else overflow.push(h);
  }
  // `out` never exceeds k, so this covers both the capped and the backfilled case.
  return out.concat(overflow).slice(0, k);
};

/** Choices offered on the first screen. See `onboardingOptions`. */
export interface OnboardingOptions {
  genres: { id: string; title: string; tracks: number }[];
  artists: { id: string; name: string; owner: string; tracks: number; followers: number }[];
}

/**
 * How many tracks each pick contributes to the seed.
 *
 * Kept small on purpose. Every seeded track is an `onPlay`, which advances the
 * kernel's timestep, and the prior pull decays as k/t — seed too heavily and the
 * kernel starts its first real session already convinced.
 */
const SEED_PER_ARTIST = 4;
const SEED_PER_GENRE = 3;

/**
 * An artist needs at least this many tracks to be worth following at onboarding.
 * Most of this corpus's famous names carry one or two tracks in the trending slice
 * (Zedd has 1), and following them would seed almost nothing.
 */
const MIN_SEED_TRACKS = 3;

/**
 * Most slots one artist may hold in a rail, before backfill.
 *
 * A display constraint, deliberately NOT part of the kernel: `reweight` is
 * sonder's and stays diffable against it, and the same weights still drive
 * autoplay via `sampleHit`, where a cap would make no sense.
 *
 * 2 against k=12 guarantees at least six distinct artists. Raise to 3 (≥4
 * artists) if the rail reads as too exploratory.
 */
const MAX_PER_ARTIST = 2;

export type SignalKind = 'play' | 'skip' | 'like' | 'dislike';

export type OnProgress = (phase: string, pct: number, detail?: string) => void;
type Embed = (text: string, opts: { pooling: 'mean' }) => Promise<{ data: Float32Array }>;

async function fetchJson<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json() as Promise<T>;
}

export class Graph {
  // Columnar on purpose: one contiguous Float32Array beats 25k small arrays for both
  // memory and scan speed.
  private vectors = new Float32Array(0);
  private recs: Rec[] = [];
  private indexById = new Map<string, number>();
  private outAdj = new Map<string, Edge[]>();
  private inAdj = new Map<string, Edge[]>();
  private platform = '';
  private _stats: Stats | null = null;
  private _embed: Promise<Embed> | null = null;
  private _onboarding: OnboardingOptions | null = null;
  /** Baked Home rows, keyed `${entityType}|${owner.toLowerCase()}`. */
  // ── the non-resident half ───────────────────────────────────────────────────
  // The bootstrap is a slice; search reaches the whole catalogue. Records that
  // arrive from a bucket or an adjacency rail live here instead of in `recs`,
  // because `recs`/`vectors` are a packed parallel array whose indices the kernel
  // and topK depend on — growing it per fetch would mean reallocating both.
  // Every read path (entity/relations/neighbours/vectorOf) consults this second.
  private remote = new Map<string, Rec>();
  private remoteVec = new Map<string, Float32Array>();
  private codebook: Int8Array | null = null;
  private layout: import('./route.ts').Layout | null = null;

  private _samples: Record<string, Rec[]> | null = null;
  private _sampleLimit = 0;
  private _edges: Edge[] = [];

  // The session kernel lives here, next to the vectors it ranks over. It starts
  // empty; the main thread rehydrates it via kernelImport() when a returning
  // visitor has saved state, because a worker cannot reach localStorage itself.
  private kernel: KernelState = emptyKernel();
  /**
   * Records already signalled on — excluded from recommendations.
   *
   * Persisted alongside the kernel, unlike `muted`. It is not a preference, but
   * dropping it makes a restored session recommend the very tracks it just
   * watched you play, which reads as the recommender echoing your history back.
   * The cost is a candidate pool that shrinks by everything you have ever
   * touched; against 25k records and demo-scale interaction that is noise.
   */
  private seen = new Set<string>();

  get stats(): Stats | null { return this._stats; }
  get size(): number { return this.recs.length; }

  /** Stream a shard, reporting real bytes so the progress bar isn't a fiction. */
  private async fetchShard(url: string, onBytes: (got: number, total: number) => void = () => {}) {
    const res = await fetch(url);
    if (!res.ok || !res.body) throw new Error(`shard ${res.status}`);
    const total = Number(res.headers.get('content-length') ?? 0);
    const chunks: Uint8Array[] = [];
    let got = 0;
    const reader = res.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value as Uint8Array);
      got += (value as Uint8Array).length;
      onBytes(got, total);
    }
    const buf = new Uint8Array(got);
    let off = 0;
    for (const c of chunks) { buf.set(c, off); off += c.length; }

    // `cdn serve` sends application/gzip with NO Content-Encoding, so fetch leaves it
    // compressed and we gunzip here. A host that DOES set Content-Encoding will have
    // decompressed it already — sniffing the magic bytes handles both without
    // double-decompressing (which throws an opaque "Failed to fetch").
    const gz = buf.length > 1 && buf[0] === 0x1f && buf[1] === 0x8b;
    if (!gz) return new TextDecoder().decode(buf);
    const stream = new Blob([buf as BlobPart]).stream().pipeThrough(new DecompressionStream('gzip'));
    return new Response(stream).text();
  }

  async load(cdn: string, domain: string, platform: string, onProgress: OnProgress = () => {}) {
    const CDN = cdn.replace(/\/$/, '');
    this.platform = platform.toLowerCase();
    let DOMAIN = domain;
    if (!DOMAIN) {
      const cat = await fetchJson<{ domains: Array<{ name: string }> }>(`${CDN}/catalog`);
      DOMAIN = cat.domains?.[0]?.name ?? 'audius';
    }

    onProgress('manifest', 2, 'Reading the snapshot manifest');
    // `stats`/`onboarding`/`samples` are written by `quickbeam cdn precompute`. They
    // are pure functions of the delivered corpus, so baking them is what lets the
    // shell paint before the shards land. All optional: a domain baked without them
    // still works, it just derives them here as before.
    const manifest = await fetchJson<{
      dim: number;
      shards: Array<{ file: string }>;
      stats?: Stats;
      catalogue?: Stats['catalogue'];
      onboarding?: OnboardingOptions;
      samples?: Record<string, Rec[]>;
      sampleLimit?: number;
    }>(`${CDN}/domains/${DOMAIN}/manifest`);
    const dim = manifest.dim ?? MATRYOSHKA_DIM;
    this._onboarding = manifest.onboarding ?? null;
    this._samples = manifest.samples ?? null;
    this._sampleLimit = manifest.sampleLimit ?? 0;

    const rows: Array<Record<string, unknown>> = [];
    for (const shard of manifest.shards) {
      const text = await this.fetchShard(`${CDN}/domains/${DOMAIN}/shards/${shard.file}`, (got, total) => {
        const frac = total ? got / total : 0;
        onProgress('shard', 4 + frac * 66,
          `${(got / 1e6).toFixed(0)} MB of ${(total / 1e6).toFixed(0)} MB`);
      });
      onProgress('parse', 72, 'Parsing records');
      for (const line of text.split('\n')) if (line.trim()) rows.push(JSON.parse(line));
    }

    this.vectors = new Float32Array(rows.length * dim);
    this.recs = new Array(rows.length);
    let n = 0;
    for (const row of rows) {
      const emb = row.embedding as number[] | undefined;
      if (!Array.isArray(emb)) continue;
      const id = String(row.track_id ?? '');
      const fields = (row.fields ?? {}) as Rec['fields'];
      this.vectors.set(emb, n * dim);
      this.recs[n] = {
        id,
        entityType: (fields.entityType as string) ?? 'Unknown',
        owner: typeof row.owner === 'string' ? row.owner : undefined,
        fields,
      };
      this.indexById.set(id, n);
      n++;
    }
    this.recs.length = n;
    if (n * dim < this.vectors.length) this.vectors = this.vectors.subarray(0, n * dim);

    onProgress('edges', 82, 'Loading the linkset');
    let edges: Edge[] = [];
    try {
      // Prefer the gzipped copy — same bytes, ~17x smaller (13.1 MB -> 0.75 MB), and
      // fetchShard's magic-byte sniff already handles both the raw and the
      // pre-decompressed case. Fall back for a domain precomputed before it existed.
      let text: string;
      try {
        text = await this.fetchShard(`${CDN}/domains/${DOMAIN}/edges.gz`);
      } catch {
        text = await this.fetchShard(`${CDN}/domains/${DOMAIN}/edges`);
      }
      edges = (JSON.parse(text) as { edges?: Edge[] }).edges ?? [];
    } catch {
      edges = []; // a domain with no linkset simply has no mesh
    }
    for (const e of edges) {
      if (!e.from || !e.to || !e.rel) continue;
      let o = this.outAdj.get(e.from); if (!o) { o = []; this.outAdj.set(e.from, o); } o.push(e);
      let i = this.inAdj.get(e.to);    if (!i) { i = []; this.inAdj.set(e.to, i); }    i.push(e);
    }

    // The public codebook, if this deployment has one. Fetched from the CDN beside the
    // shards — it is public by design: the privacy of the scheme rests on the disclosed
    // value being a deterministic public function, never on the codebook being secret.
    // An auditor should be able to fetch exactly what the client routes against.
    if (API_URL) {
      try {
        onProgress('index', 92, 'Reading the public codebook');
        const [layout, codebook] = await Promise.all([
          store.fetchLayout(CDN, INDEX_DOMAIN || DOMAIN),
          store.fetchCodebook(CDN, INDEX_DOMAIN || DOMAIN),
        ]);
        this.layout = layout;
        this.codebook = codebook;
      } catch {
        // No index served → search stays local. The app is still correct, just
        // limited to what it downloaded, which is exactly audius-demo's behaviour.
        this.layout = null;
        this.codebook = null;
      }
    }

    onProgress('stats', 94, 'Summarising the publishers');
    this._edges = edges;
    // Prefer the baked value; buildStats stays reachable both as the fallback for an
    // un-precomputed domain and so check-graph can assert the two agree.
    this._stats = manifest.stats ?? this.buildStats(edges);
    // The catalogue block is written by bootstrap.py and describes the FULL corpus,
    // not the slice that was downloaded. Attached here so every consumer of `stats`
    // can reach it without a second fetch.
    if (this._stats && manifest.catalogue) this._stats.catalogue = manifest.catalogue;
    onProgress('ready', 100);
    return this._stats;
  }

  ownerOf(id: string): string | undefined {
    const i = this.indexById.get(id);
    return i === undefined ? undefined : this.recs[i].owner;
  }

  /**
   * Genre/Mood/Tag vertices, which BOTH publishers derive identically and content
   * addressing therefore collapses into one record.
   *
   * That record can only carry one `owner`, so any edge into it from the other
   * publisher looks like it crosses a boundary when it doesn't — it's convergence,
   * where a shared vertex is the whole point. Anything reasoning about
   * cross-publisher joins has to skip these or it overstates the linkset wildly.
   */
  private isVocab(id: string): boolean {
    const i = this.indexById.get(id);
    return i !== undefined && !!this.recs[i].fields.vocabulary;
  }

  private buildStats(edges: Edge[]): Stats {
    const byOwner = new Map<string, Record<string, number>>();
    for (const r of this.recs) {
      const o = r.owner ?? 'unknown';
      let c = byOwner.get(o); if (!c) { c = {}; byOwner.set(o, c); }
      c[r.entityType] = (c[r.entityType] ?? 0) + 1;
    }

    const publishers = [...byOwner.entries()]
      .map(([owner, counts]) => {
        // Name a publisher from its own Artist record. Nothing hard-codes WHICH
        // artist this demo is about — change the focus artist and this follows.
        const own = this.recs.filter(
          (r) => r.owner === owner && r.entityType === 'Artist' && !r.fields.isReference);
        return {
          owner, counts,
          total: Object.values(counts).reduce((a, b) => a + b, 0),
          label: own.length === 1 ? (own[0].fields.title as string | undefined) : undefined,
          labelId: own.length === 1 ? own[0].id : undefined,
        };
      })
      // Platform first, so the ledger reads left-to-right as platform → artist.
      .sort((a, b) =>
        a.owner.toLowerCase() === this.platform ? -1
          : b.owner.toLowerCase() === this.platform ? 1
            : b.total - a.total);

    // Cross-publisher edges. Vocabulary endpoints are excluded (see `isVocab`) or
    // this number is wrong by two orders of magnitude — it reported 12,657 crossing
    // edges against a real linkset of 113.
    const linksetBy = new Map<string, number>();
    for (const e of edges) {
      if (this.isVocab(e.from) || this.isVocab(e.to)) continue;
      const a = this.ownerOf(e.from), b = this.ownerOf(e.to);
      if (a && b && a !== b) linksetBy.set(e.rel, (linksetBy.get(e.rel) ?? 0) + 1);
    }
    const linkset = [...linksetBy.entries()]
      .map(([rel, count]) => ({ rel, count }))
      .sort((a, b) => b.count - a.count);

    // Vocabulary vertices (Genre/Mood/Tag) that BOTH publishers independently arrived
    // at. Each side derives these from its own tracks; identical payload bytes mean
    // content addressing hands both publishers the SAME CID, so the graphs merge
    // there with no linkset entry and no coordination.
    //
    // Counted honestly: a vertex qualifies only when records from two DIFFERENT
    // owners point at it. Counting every vocabulary node would be a bigger number
    // and a false claim — most are reached by one publisher only.
    const vocabOwners = new Map<string, Set<string>>();
    for (const e of edges) {
      if (!this.isVocab(e.to)) continue;
      const from = this.ownerOf(e.from);
      if (!from) continue;
      let s = vocabOwners.get(e.to);
      if (!s) { s = new Set(); vocabOwners.set(e.to, s); }
      s.add(from);
    }
    let converged = 0;
    for (const s of vocabOwners.values()) if (s.size > 1) converged++;

    return {
      records: this.recs.length,
      edges: edges.length,
      publishers,
      linkset,
      linksetTotal: linkset.reduce((a, b) => a + b.count, 0),
      converged,
    };
  }

  // ── embedding ──────────────────────────────────────────────────────────────
  private embedder(): Promise<Embed> {
    return (this._embed ??= (async () => {
      // transformers.js declares `pipeline` as an overload union across every task
      // it supports, which TS can't represent when instantiated (TS2590). We use
      // exactly one task in one calling form, so both are narrowed here.
      const mod = (await import('@huggingface/transformers')) as unknown as {
        pipeline: (task: string, model: string, opts: { dtype: string }) => Promise<Embed>;
      };
      // q8 keeps the download small; at 256 dims the quality difference is noise.
      return mod.pipeline('feature-extraction', 'nomic-ai/nomic-embed-text-v1.5', { dtype: 'q8' });
    })());
  }

  async embedQuery(text: string): Promise<Float32Array> {
    const pipe = await this.embedder();
    const out = await pipe(QUERY_PREFIX + text, { pooling: 'mean' });
    return matryoshka(out.data, MATRYOSHKA_DIM);
  }

  // ── queries ────────────────────────────────────────────────────────────────
  private toRec(i: number, score?: number): Rec {
    const r = this.recs[i];
    return { id: r.id, entityType: r.entityType, owner: r.owner, fields: r.fields, score };
  }

  /** Absorb rows fetched from a bucket or an adjacency rail. */
  private absorb(rows: store.BucketRow[]): Rec[] {
    const out: Rec[] = [];
    for (const row of rows) {
      const fields = (row.fields ?? {}) as Rec['fields'];
      const rec: Rec = {
        id: row.id,
        entityType: (fields.entityType as string) ?? 'Unknown',
        owner: typeof row.owner === 'string' ? row.owner : undefined,
        fields,
      };
      // A record already resident wins: same id, same bytes, but the resident copy
      // is the one topK and the kernel index against.
      if (!this.indexById.has(rec.id)) {
        this.remote.set(rec.id, rec);
        if (Array.isArray(row.embedding)) {
          this.remoteVec.set(rec.id, Float32Array.from(row.embedding));
        }
      }
      out.push(rec);
    }
    return out;
  }

  /**
   * Search the WHOLE catalogue, not just what is resident.
   *
   * The query is embedded here, routed against the public codebook here, and only a
   * bucket id crosses the network (see store.ts — the one file in this path that
   * touches fetch). Candidates come back with vectors and are re-ranked against the
   * true query locally, which is what makes the disclosed value one integer instead
   * of the query itself.
   *
   * Falls back to the resident scan when the index is not configured, so the app
   * still works against a bootstrap-only deployment.
   */
  async search(q: string, k: number, type?: string): Promise<Rec[]> {
    const vec = await this.embedQuery(q);
    const local = topK(this.vectors, this.recs.length, vec,
                       k, type ? (i: number) => this.recs[i].entityType === type : undefined)
      .map((h) => this.toRec(h.index, h.score));

    if (!API_URL || !this.layout || !this.codebook) return local;

    let rows: store.BucketRow[];
    try {
      rows = await store.fetchBuckets(API_URL,
        route.bucketsFor(this.codebook, this.layout, vec, NPROBE));
    } catch {
      return local;   // a dead index must degrade to the bootstrap, not to nothing
    }
    const remote = this.absorb(rows);
    const scale = this.layout.vector_scale;
    const scored: Rec[] = [];
    for (const r of remote) {
      if (type && r.entityType !== type) continue;
      const v = this.remoteVec.get(r.id) ?? this.vectorOf(r.id);
      if (!v) continue;
      let dot = 0;
      for (let d = 0; d < vec.length; d++) dot += v[d] * vec[d];
      scored.push({ ...r, score: dot });
    }
    // Merge with the resident hits and dedupe by id. Both sides carry the same
    // vectors for the same records — the bootstrap is a slice of the same build —
    // so a record present in both scores identically and the dedupe is safe.
    const byId = new Map<string, Rec>();
    for (const r of [...local, ...scored]) {
      const prev = byId.get(r.id);
      if (!prev || (r.score ?? 0) > (prev.score ?? 0)) byId.set(r.id, r);
    }
    void scale;
    return [...byId.values()].sort((a, b) => (b.score ?? 0) - (a.score ?? 0)).slice(0, k);
  }

  /** Resident first, then anything a bucket or rail has already handed us. */
  entity(id: string): Rec | null {
    const i = this.indexById.get(id);
    if (i !== undefined) return this.toRec(i);
    return this.remote.get(id) ?? null;
  }

  /** Like `entity`, but will go and get it. Used by the entity view, where a cold
   *  deep link or a refresh has nothing cached and would otherwise render
   *  "Not in this snapshot" for a record that plainly exists. */
  async entityAsync(id: string): Promise<Rec | null> {
    const hit = this.entity(id);
    if (hit || !API_URL) return hit;
    try {
      const rows = await store.fetchRecords(API_URL, [id]);
      return this.absorb(rows)[0] ?? null;
    } catch {
      return null;
    }
  }

  relations(id: string): RelationGroup[] {
    const mine = this.ownerOf(id);
    const seen = new Map<string, RelationGroup>();
    for (const dir of ['out', 'in'] as const) {
      for (const e of (dir === 'out' ? this.outAdj.get(id) : this.inAdj.get(id)) ?? []) {
        const key = `${dir}:${e.rel}`;
        const otherId = dir === 'out' ? e.to : e.from;
        const other = this.ownerOf(otherId);
        let g = seen.get(key);
        if (!g) { g = { rel: e.rel, dir, count: 0, crosses: false }; seen.set(key, g); }
        g.count++;
        // A shared vocabulary vertex is convergence, not a cross-publisher join —
        // flagging `inGenre` as "crosses publishers" would be a false claim on
        // nearly every track page.
        if (mine && other && other !== mine && !this.isVocab(otherId)) g.crosses = true;
      }
    }
    // Cross-publisher rails first: they're the demonstration, not a footnote.
    return [...seen.values()]
      .sort((a, b) => Number(b.crosses) - Number(a.crosses) || b.count - a.count);
  }

  /** Relation groups, falling back to the adjacency service for a record whose
   *  edges are not in the resident linkset. Without this a search result opens to
   *  "No relations recorded for this node" even though the graph holds thousands. */
  async relationsAsync(id: string): Promise<RelationGroup[]> {
    const local = this.relations(id);
    if (local.length || !API_URL) return local;
    try {
      return (await store.fetchRelations(API_URL, id)) as unknown as RelationGroup[];
    } catch {
      return local;
    }
  }

  async neighboursAsync(id: string, rel: string, dir: 'out' | 'in', limit: number) {
    const local = this.neighbours(id, rel, dir, limit);
    if (local.records.length || !API_URL) return local;
    try {
      const rows = await store.fetchNeighbours(API_URL, id, rel, dir, limit);
      const records = this.absorb(rows);
      return { records, total: records.length };
    } catch {
      return local;
    }
  }

  neighbours(id: string, rel: string, dir: 'out' | 'in', limit: number) {
    const es = (dir === 'out' ? this.outAdj.get(id) : this.inAdj.get(id)) ?? [];
    const ids: string[] = [];
    const seen = new Set<string>();
    for (const e of es) {
      if (e.rel !== rel) continue;
      const nid = dir === 'out' ? e.to : e.from;
      if (seen.has(nid)) continue;
      seen.add(nid);
      ids.push(nid);
    }
    const records: Rec[] = [];
    for (const nid of ids.slice(0, limit)) {
      const i = this.indexById.get(nid);
      if (i !== undefined) records.push(this.toRec(i));
    }
    return { records, total: ids.length };
  }

  // ── session kernel ─────────────────────────────────────────────────────────
  /** A record's vector as a standalone Float32Array (a copy, not a view). */
  private vectorOf(id: string): Float32Array | null {
    const i = this.indexById.get(id);
    if (i !== undefined) {
      const d = MATRYOSHKA_DIM;
      return this.vectors.slice(i * d, (i + 1) * d);
    }
    // Bucket and adjacency rows carry their vectors. Without this the kernel goes
    // deaf the moment you play something you found by searching: kernelSignal
    // no-ops when it cannot find a vector, so playback looks perfectly normal
    // while the recommender learns nothing at all.
    return this.remoteVec.get(id) ?? null;
  }

  /**
   * Feed the kernel one signal.
   *
   * `like`/`dislike` route to the same transitions as `play`/`skip` on purpose:
   * there is one state machine, and an explicit thumb is just an unambiguous
   * instance of the same evidence. Keeping two would mean two things to reason
   * about and two things to get out of sync.
   */
  kernelSignal(id: string, kind: SignalKind): ReturnType<typeof describe> {
    const rec = this.entity(id);
    const vec = this.vectorOf(id);
    if (rec && vec) {
      const feat = toFeatures(rec, vec);
      this.kernel = (kind === 'skip' || kind === 'dislike')
        ? onSkip(this.kernel, feat)
        : onPlay(this.kernel, feat);
      this.seen.add(id);
    }
    return describe(this.kernel);
  }

  kernelReset(): ReturnType<typeof describe> {
    this.kernel = emptyKernel();
    this.seen.clear();
    return describe(this.kernel);
  }

  kernelSnapshot() {
    return { ...describe(this.kernel), signals: this.seen.size };
  }

  /**
   * Everything a returning visitor needs. `state` is sonder's own serialisation
   * (which omits `muted` by design — session fatigue clears on a cold start);
   * `seen` rides alongside it because it lives here, not in the kernel.
   */
  kernelExport(): KernelPersist {
    return { state: serialiseState(this.kernel), seen: [...this.seen] };
  }

  /**
   * Adopt previously-saved state. Safe to call before `load()` resolves — the
   * kernel is self-contained and touches none of the shard-derived fields.
   */
  kernelImport(p: KernelPersist): ReturnType<typeof describe> {
    this.kernel = deserialiseState(p.state);
    this.seen = new Set(p.seen ?? []);
    return describe(this.kernel);
  }

  /**
   * Take records in the given order, at most `per` from any one artist.
   *
   * Every derived rail in this file needs it, for the reason the recommendation
   * rail did: an artist's catalogue clusters tightly, so the nearest N of
   * anything is otherwise one artist repeated. `ranked` is [recordIndex, score].
   */
  private capByArtist(ranked: Array<[number, number]>, k: number, per = 2): Rec[] {
    const seen = new Map<string, number>();
    const out: Rec[] = [];
    for (const [i, score] of ranked) {
      const a = String(this.recs[i].fields.artistId ?? this.recs[i].id);
      const n = seen.get(a) ?? 0;
      if (n >= per) continue;
      seen.set(a, n + 1);
      out.push(this.toRec(i, score));
      if (out.length >= k) break;
    }
    return out;
  }

  /** Edges of one relation leaving/entering a node, as record indices. */
  private linked(id: string, rel: string, dir: 'out' | 'in'): number[] {
    const es = (dir === 'out' ? this.outAdj : this.inAdj).get(id) ?? [];
    const out: number[] = [];
    for (const e of es) {
      if (e.rel !== rel) continue;
      const i = this.indexById.get(dir === 'out' ? e.to : e.from);
      if (i !== undefined) out.push(i);
    }
    return out;
  }

  /**
   * What the first screen offers. Computed once and cached — it walks every Genre
   * and Artist record, which is cheap against adjacency maps but pointless twice.
   *
   * Artists are ranked by followers among those holding enough tracks to seed with,
   * NOT by track count: the most prolific accounts here are bulk uploaders nobody
   * recognises (633 tracks, 37 followers), which makes for a picker of strangers.
   */
  onboardingOptions(genreN = 12, artistN = 12): OnboardingOptions {
    if (this._onboarding) return this._onboarding;
    const count = (id: string, rel: string, dir: 'out' | 'in') => this.linked(id, rel, dir).length;

    const genres = this.recs
      .filter((r) => r.entityType === 'Genre')
      .map((r) => ({
        id: r.id,
        title: String(r.fields.title ?? r.fields.id ?? ''),
        tracks: count(r.id, 'inGenre', 'in'),
      }))
      .filter((g) => g.tracks > 0 && g.title)
      .sort((a, b) => b.tracks - a.tracks)
      .slice(0, genreN);

    const artists = this.recs
      .filter((r) => r.entityType === 'Artist')
      .map((r) => ({
        id: r.id,
        name: String(r.fields.artist ?? ''),
        owner: r.owner ?? '',
        tracks: count(r.id, 'created', 'out'),
        followers: Number(r.fields.followerCount || 0),
      }))
      .filter((a) => a.tracks >= MIN_SEED_TRACKS && a.name)
      .sort((a, b) => b.followers - a.followers)
      .slice(0, artistN);

    return (this._onboarding = { genres, artists });
  }

  /**
   * Start a kernel from stated preferences rather than from listening.
   *
   * Each pick contributes its most-reposted tracks as `onPlay` transitions, so the
   * kernel lands in exactly the state a real session would have reached — no new
   * maths, and taste/artist-affinity/μ all move together as they normally do.
   *
   * Seeded tracks are deliberately NOT added to `seen`: you followed these artists,
   * so their work is the first thing the rail should be allowed to recommend.
   */
  kernelSeed(genreIds: string[], artistIds: string[]): ReturnType<typeof describe> {
    const byReposts = (a: number, b: number) =>
      Number(this.recs[b].fields.repostCount || 0) - Number(this.recs[a].fields.repostCount || 0);
    const pick = (id: string, rel: string, dir: 'out' | 'in', n: number) =>
      this.linked(id, rel, dir)
        .filter((i) => this.recs[i].entityType === 'Track')
        .sort(byReposts)
        .slice(0, n);

    // ORDER IS LOAD-BEARING, and not obviously so.
    //
    // μ is an EMA, so the last plays decide where the kernel actually ends up. And
    // `kernelRecommend` shortlists by raw cosine against μ *before* reweighting, so
    // a followed artist whose tracks fall outside that shortlist never receives
    // their tau_art boost at all — following them would do nothing.
    //
    // Genres are the backdrop and go first. The artists chosen explicitly go last,
    // round-robined so the tail touches every one of them rather than whichever
    // happened to be listed third.
    const order: number[] = [];
    const added = new Set<number>();
    const push = (i: number) => { if (!added.has(i)) { added.add(i); order.push(i); } };

    for (const g of genreIds) for (const i of pick(g, 'inGenre', 'in', SEED_PER_GENRE)) push(i);
    const perArtist = artistIds.map((a) => pick(a, 'created', 'out', SEED_PER_ARTIST));
    for (let n = 0; n < SEED_PER_ARTIST; n++) {
      for (const list of perArtist) if (list[n] !== undefined) push(list[n]);
    }

    let k = emptyKernel();
    for (const i of order) {
      const vec = this.vectorOf(this.recs[i].id);
      if (vec) k = onPlay(k, toFeatures(this.toRec(i), vec));
    }
    this.kernel = k;
    this.seen.clear();
    return describe(this.kernel);
  }

  /**
   * Discovery rails for an artist page, along BOTH axes of the mesh.
   *
   * The graph has no `relatedTo` edge — nothing in the source data says two
   * artists belong together — so relatedness has to be derived. Two independent
   * derivations, kept separate rather than blended, because they are different
   * claims and the page says so:
   *
   *   peers    RELATIONAL. Artists whose tracks share a playlist with this
   *            artist's, ranked by how often. Real `contains` edges, walked. For
   *            the sovereign artist every peer is a platform artist, because
   *            their tracks sit in the platform's playlists — so this rail
   *            crosses the publisher boundary by construction, which is the
   *            demo's whole claim showing up as a discovery feature.
   *            Only ~57% of artists have any: playlists are sparse here.
   *
   *   similar  SEMANTIC. Tracks nearest the centroid of this artist's own
   *            catalogue. Always available, which is why it exists — it covers
   *            the artists `peers` cannot reach.
   *
   * Note `similar` is built from TRACK vectors, never artist vectors. An Artist
   * record embeds its bio ("…is an artist on Audius. Based in London, U.K.")
   * so artist-to-artist similarity ranks press blurbs, not music: it puts Zedd
   * near Skrillex by luck and returns noise for anyone with a thin bio. The
   * track centroid describes what they actually sound like.
   */
  discovery(id: string, k = 12): { peers: Rec[]; similar: Rec[] } {
    const rec = this.entity(id);
    if (rec?.entityType === 'Artist') return this.artistDiscovery(id, k);
    if (rec?.entityType === 'Track') return this.trackDiscovery(rec, k);
    return { peers: [], similar: [] };
  }

  /**
   * A track's two neighbourhoods.
   *
   *   peers    Tracks that share a playlist with this one, ranked by how many
   *            playlists they share. Real `contains` edges — the same curatorial
   *            signal the artist rail uses, one level down. Covers ~89% of
   *            tracks, the widest relational reach in this graph (favourites
   *            reach only ~14%, which is why they are not used).
   *
   *   similar  Nearest tracks by embedding. Covers everything, including the
   *            ~11% that appear in no playlist at all.
   */
  private trackDiscovery(rec: Rec, k: number): { peers: Rec[]; similar: Rec[] } {
    const self = this.indexById.get(rec.id);
    if (self === undefined) return { peers: [], similar: [] };

    const tally = new Map<number, number>();
    for (const p of this.linked(rec.id, 'contains', 'in')) {
      for (const s of this.linked(this.recs[p].id, 'contains', 'out')) {
        if (s === self || this.recs[s].entityType !== 'Track') continue;
        tally.set(s, (tally.get(s) ?? 0) + 1);
      }
    }
    const peers = this.capByArtist([...tally.entries()].sort((a, b) => b[1] - a[1]), k);

    const v = this.vectorOf(rec.id);
    const similar = v
      ? this.capByArtist(
          topK(this.vectors, this.recs.length, v, k * 20,
               (i) => this.recs[i].entityType === 'Track' && i !== self)
            .map((h) => [h.index, h.score] as [number, number]),
          k)
      : [];

    return { peers, similar };
  }

  private artistDiscovery(id: string, k: number): { peers: Rec[]; similar: Rec[] } {
    const mine = this.linked(id, 'created', 'out')
      .filter((i) => this.recs[i].entityType === 'Track');
    if (!mine.length) return { peers: [], similar: [] };

    // ── relational: playlist co-occurrence ────────────────────────────────
    // Creator resolved by walking `created` back from each sibling rather than
    // rebuilding an "audius:user:<id>" string — the edge is the source of truth.
    const tally = new Map<number, number>();
    for (const t of mine) {
      for (const p of this.linked(this.recs[t].id, 'contains', 'in')) {
        for (const s of this.linked(this.recs[p].id, 'contains', 'out')) {
          if (this.recs[s].entityType !== 'Track') continue;
          for (const a of this.linked(this.recs[s].id, 'created', 'in')) {
            if (this.recs[a].id === id) continue;
            tally.set(a, (tally.get(a) ?? 0) + 1);
          }
        }
      }
    }
    const peers = [...tally.entries()]
      .sort((x, y) => y[1] - x[1])
      .slice(0, k)
      .map(([i, n]) => this.toRec(i, n));

    // ── semantic: nearest tracks to this artist's centroid ────────────────
    const d = MATRYOSHKA_DIM;
    const c = new Float32Array(d);
    for (const i of mine) {
      const v = this.vectorOf(this.recs[i].id);
      if (v) for (let j = 0; j < d; j++) c[j] += v[j];
    }
    let norm = 0;
    for (let j = 0; j < d; j++) norm += c[j] * c[j];
    norm = Math.sqrt(norm) || 1;
    for (let j = 0; j < d; j++) c[j] /= norm;

    const own = new Set(mine.map((i) => this.recs[i].id));
    const similar = this.capByArtist(
      topK(this.vectors, this.recs.length, c, k * 20,
           (i) => this.recs[i].entityType === 'Track' && !own.has(this.recs[i].id))
        .map((h) => [h.index, h.score] as [number, number]),
      k);

    return { peers, similar };
  }

  /**
   * Where the session is heading: rank the corpus by the kernel's query vector,
   * then let the kernel reweight the shortlist.
   *
   * Deterministic top-k rather than `sampleHit` — a rail must not reshuffle while
   * someone is looking at it. `sampleHit` remains the right call for autoplay.
   */
  kernelRecommend(k = 12, owner?: string, pool = 600): Rec[] {
    if (this.kernel.t === 0 && this.kernel.skips.length === 0) return [];
    const q = kernelQuery(this.kernel);

    // `owner` narrows the SAME ranking to one publisher — it does not re-rank.
    // If it ranks nothing from over there, this returns nothing; the rail is
    // simply absent rather than flattered into existence.
    //
    // This used to be the ONLY way the demo showed a crossing: `tau_art` made
    // the top 12 all one artist and crossing began around k=40, so a rail-sized
    // k never left the publisher you started on. MAX_PER_ARTIST changed that —
    // the main rail now mixes publishers at k=12 on its own.
    //
    // Still worth keeping, for a reason worth stating: this publisher has ONE
    // artist record, so the cap allows their repo at most MAX_PER_ARTIST slots
    // in any mixed rail no matter how strong the affinity. This filtered rail is
    // where their catalogue can actually be seen at depth, and it says "the same
    // weights, narrowed" — which a mixed rail cannot state that directly.
    const want = owner?.toLowerCase();
    const accept = (i: number) => {
      const r = this.recs[i];
      if (this.seen.has(r.id)) return false;
      if (r.entityType !== 'Track' && r.entityType !== 'Artist') return false;
      return !want || (r.owner ?? '').toLowerCase() === want;
    };
    const shortlist = topK(this.vectors, this.recs.length, q, pool, accept);
    if (!shortlist.length) return [];

    const hits: Hit[] = shortlist.map(({ index, score }) => {
      const rec = this.recs[index];
      const emb = this.vectorOf(rec.id)!;
      const f = toFeatures(rec, emb);
      return {
        id: rec.id,
        embedding: emb,
        // cosine → squared L2, which is what reweight's Gaussian expects.
        distance: cosToDistance(score),
        metadata: {
          artistId: f.artistId,
          genres: f.genres,
          moods: f.moods,
          themes: f.themes,
          contexts: f.contexts,
          durationMs: f.durationMs,
        },
      };
    });

    const ranked = reweight(hits, this.kernel).sort((a, b) => b.weight - a.weight);
    return capPerArtist(ranked, k, MAX_PER_ARTIST).map((h) => {
      const i = this.indexById.get(h.id)!;
      return this.toRec(i, h.weight);
    });
  }

  /**
   * Best-known records of a type, optionally from one publisher.
   *
   * The owner filter matters on the home page: the artist holds ~99 of 25k records,
   * so an unfiltered "most played" row would be entirely platform-owned and the
   * landing page would quietly argue against its own headline.
   */
  /** Recompute the shell summary from the resident corpus, ignoring anything the
   *  manifest supplied. Exists so check-graph can assert that the baked values and
   *  this code agree — a second implementation with no equivalence test drifts. */
  recomputeStats(): Stats { return this.buildStats(this._edges); }

  sample(type: string, limit: number, owner?: string): Rec[] {
    const want = owner?.toLowerCase();
    // Serve from the baked slice only when it can answer the exact question asked.
    // The node checks call sample() with limits up to 600 and types the bake does
    // not cover, so the computed path below has to stay — this is a fast path, not
    // a replacement.
    if (want && this._samples && limit <= this._sampleLimit) {
      const baked = this._samples[`${type}|${want}`];
      if (baked) return baked.slice(0, limit).map((r) => ({ ...r }));
    }
    const pick = this.recs.filter(
      (r) => r.entityType === type && (!want || (r.owner ?? '').toLowerCase() === want));
    const num = (r: Rec) => Number(r.fields.playCount ?? r.fields.followerCount ?? 0);
    pick.sort((a, b) => num(b) - num(a));
    return pick.slice(0, limit).map((r) => ({ ...r }));
  }
}
