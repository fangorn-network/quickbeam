// The graph itself: loading the snapshot, embedding a query, scoring, and walking
// the linkset. Deliberately free of worker globals (`self`, `postMessage`) so the
// same code runs under node — which is how it gets tested without a browser. The
// worker is a thin message shim over this.
// Explicit .ts extension: node's ESM resolver requires it, and this module is
// imported directly by the node-side checks as well as bundled by vite.
import { MATRYOSHKA_DIM, QUERY_PREFIX, matryoshka, topK } from './matryoshka.ts';
import type { Edge, Rec, RelationGroup, Stats } from './types.ts';
import { cosToDistance, toFeatures } from '../kernel/adapt.ts';
import {
  describe, emptyKernel, onPlay, onSkip, queryVector as kernelQuery, reweight,
} from '../kernel/SessionKernel.ts';
import type { Hit, KernelState } from '../kernel/types.ts';

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

  // The session kernel lives here, next to the vectors it ranks over. Session-only:
  // no persistence, so every demo starts from a clean state.
  private kernel: KernelState = emptyKernel();
  /** Records already signalled on — excluded from recommendations. */
  private seen = new Set<string>();

  get stats(): Stats | null { return this._stats; }
  get size(): number { return this.recs.length; }

  /** Stream a shard, reporting real bytes so the progress bar isn't a fiction. */
  private async fetchShard(url: string, onBytes: (got: number, total: number) => void) {
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
    const manifest = await fetchJson<{ dim: number; shards: Array<{ file: string }> }>(
      `${CDN}/domains/${DOMAIN}/manifest`,
    );
    const dim = manifest.dim ?? MATRYOSHKA_DIM;

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
      const j = await fetchJson<{ edges?: Edge[] }>(`${CDN}/domains/${DOMAIN}/edges`);
      edges = j.edges ?? [];
    } catch {
      edges = []; // a domain with no linkset simply has no mesh
    }
    for (const e of edges) {
      if (!e.from || !e.to || !e.rel) continue;
      let o = this.outAdj.get(e.from); if (!o) { o = []; this.outAdj.set(e.from, o); } o.push(e);
      let i = this.inAdj.get(e.to);    if (!i) { i = []; this.inAdj.set(e.to, i); }    i.push(e);
    }

    onProgress('stats', 94, 'Summarising the publishers');
    this._stats = this.buildStats(edges);
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

  async search(q: string, k: number, type?: string): Promise<Rec[]> {
    const vec = await this.embedQuery(q);
    const accept = type ? (i: number) => this.recs[i].entityType === type : undefined;
    return topK(this.vectors, this.recs.length, vec, k, accept)
      .map((h) => this.toRec(h.index, h.score));
  }

  entity(id: string): Rec | null {
    const i = this.indexById.get(id);
    return i === undefined ? null : this.toRec(i);
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
    if (i === undefined) return null;
    const d = MATRYOSHKA_DIM;
    return this.vectors.slice(i * d, (i + 1) * d);
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
    //
    // Needed because the kernel is a good recommender and therefore a boring
    // demonstrator: `tau_art` rightly favours the artist you just played, so after
    // three Disclosure tracks the top 12 are all Disclosure (they have ~38 more).
    // Crossing only starts around k=40. Filtering the same weights to the other
    // publisher shows the boundary isn't a fence without distorting the model —
    // if it ranks nothing from over there, this returns nothing.
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

    return reweight(hits, this.kernel)
      .sort((a, b) => b.weight - a.weight)
      .slice(0, k)
      .map((h) => {
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
  sample(type: string, limit: number, owner?: string): Rec[] {
    const want = owner?.toLowerCase();
    const pick = this.recs.filter(
      (r) => r.entityType === type && (!want || (r.owner ?? '').toLowerCase() === want));
    const num = (r: Rec) => Number(r.fields.playCount ?? r.fields.followerCount ?? 0);
    pick.sort((a, b) => num(b) - num(a));
    return pick.slice(0, limit).map((r) => ({ ...r }));
  }
}
