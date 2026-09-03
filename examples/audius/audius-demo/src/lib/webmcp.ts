// WebMCP — this tab as agent tools.
//
// https://github.com/webmachinelearning/webmcp: the page registers tools on
// `document.modelContext` and a browser-resident agent calls them. No server, no
// transport, no dependency — the tools ARE these functions, running in the tab with
// the whole snapshot already downloaded.
//
// This app already has two clients for one snapshot: the browser, and `quickbeam mcp`
// (a Python pull-client, see audius-source/README.md). That README's argument for the
// pull-client — "the snapshot comes to the process and the queries never leave it" —
// is made STRONGEST here, because there is no process. The tab already holds the
// shards, so an agent's query is answered by the same local vector search a person's
// is, and nothing about either reaches a server. An out-of-process MCP server would
// re-download the corpus and would see every query.
//
// The tools past the graph ones are the reason to do this in the page at all:
// `control-player` moves the SAME <audio> element the person's transport bar drives,
// and `read-taste` reads a session kernel that exists in this tab and nowhere else —
// there is no profile on a server to look up. No backend can reach either.
//
// ZERO imports, like playlists.ts and for the same reason: scripts/check-webmcp.ts
// loads this module under `node --experimental-strip-types`, and client.ts builds a
// `Worker` at module scope, which throws there. Everything — the client functions, the
// player, the kernel, the playlist store, the router — arrives through `get()`.
//
// ponytail: NO tool deletes or renames anything, and the only two that write to the
// person's library are create-playlist and add-to-playlist. See `Deps.confirm`.

// ── the API, which lib.dom.d.ts does not know about yet ─────────────────────

export interface ToolResult {
  content: Array<{ type: 'text'; text: string }>;
}

/**
 * What an agent passed. `any` is the honest type, not laziness: these values arrive as
 * arbitrary JSON from outside the app — the schema is a hint to the model, not a
 * guarantee — so nothing here can be trusted to have the declared type. Every tool
 * coerces at its first line (String/Number, or an Array.isArray filter), and THAT is
 * the validation. Typing it `unknown` would only move the same casts around.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type ToolArgs = Record<string, any>;

export interface Tool {
  name: string;
  description: string;
  /** Raw JSON Schema. No Zod — the browser takes the schema as-is. */
  inputSchema: {
    type: 'object';
    properties: Record<string, unknown>;
    required?: string[];
  };
  execute(args: ToolArgs): Promise<ToolResult>;
}

export interface ModelContext {
  registerTool(tool: Tool, opts?: { signal?: AbortSignal }): void;
}

declare global {
  // eslint-disable-next-line @typescript-eslint/consistent-type-definitions
  interface Document {
    modelContext?: ModelContext;
  }
}

// ── the world these tools read ──────────────────────────────────────────────

/** Minimal structural shapes. Deliberately not imported from types.ts — this module
 *  stays import-free (see the header), and structural typing makes the real `Rec`
 *  assignable to this anyway. */
export interface RecLike {
  id: string;
  entityType: string;
  owner?: string;
  score?: number;
  fields: Record<string, unknown>;
}

export interface PlaylistLike {
  id: string;
  name: string;
  trackIds: string[];
}

/**
 * Everything the tools need, read through `get()` on every call rather than captured
 * at registration — so a tool registered at mount still sees the snapshot that
 * finished loading afterwards, and the playlist the person made a moment ago.
 */
export interface Deps {
  // the graph (client.ts)
  search(q: string, k?: number, type?: string): Promise<RecLike[]>;
  entity(id: string): Promise<RecLike | null>;
  entities(ids: string[]): Promise<(RecLike | null)[]>;
  relations(id: string): Promise<Array<{ rel: string; dir: 'out' | 'in'; count: number; crosses: boolean }>>;
  neighbours(id: string, rel: string, dir: 'out' | 'in', limit?: number):
    Promise<{ records: RecLike[]; total: number }>;
  sample(type: string, limit?: number, owner?: string): Promise<RecLike[]>;
  stats(): Promise<unknown>;

  // the session kernel
  taste(): Promise<unknown>;
  recommend(k?: number): Promise<RecLike[]>;

  // the player the person is listening to
  player(): {
    current: RecLike | null;
    playing: boolean;
    time: number;
    duration: number;
    play(rec: RecLike, queue?: RecLike[]): void;
    toggle(rec?: RecLike): void;
    seek(t: number): void;
    next(): void;
    prev(): void;
  };

  // the person's library
  playlists(): PlaylistLike[];
  /** Both go through the store's own merge, so neither can rename or remove.
   *  create returns the playlist itself rather than an id to look up: React has not
   *  re-rendered when it returns, so `playlists()` would not contain it yet. */
  createPlaylist(name: string, trackIds: string[]): PlaylistLike;
  addToPlaylist(id: string, trackIds: string[]): { added: number };
  shareUrl(pls: PlaylistLike[]): string;

  /** Put a card on the screen and wait for a press. Resolves false on dismissal or
   *  timeout — silence is never consent. */
  confirm(question: string, detail: string): Promise<boolean>;

  // moving the page the person is looking at
  goSearch(q: string): void;
  goEntity(id: string): void;
  goPlaylists(id?: string): void;
}

// ── helpers ─────────────────────────────────────────────────────────────────

/** MCP content block. Objects go as pretty JSON so an agent reads them without a
 *  parse step. */
const text = (v: unknown): ToolResult => ({
  content: [{
    type: 'text',
    text: typeof v === 'string' ? v : JSON.stringify(v, null, 1),
  }],
});

const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
const str = (v: unknown) => (typeof v === 'string' ? v : undefined);

/**
 * The projection every record-returning tool uses.
 *
 * The field list is a REQUIREMENT, not a size optimisation. `duration` and `mood` are
 * what let an agent COMPOSE rather than merely retrieve: without duration it cannot
 * answer "one hour long", and without mood it is guessing at energy from titles.
 * Measured against the bake — 1,928 of 1,929 tracks carry a duration (median 188s, so
 * ~19 fill an hour), and mood is a controlled vocabulary that happens to be an energy
 * gradient: Energizing / Fiery / Upbeat through Cool / Easygoing down to Peaceful /
 * Melancholy.
 *
 * Everything else about a record (artwork CIDs, play counts, the embedded text) is
 * omitted: it costs an agent context and buys it nothing.
 */
export function brief(r: RecLike) {
  const f = r.fields;
  return {
    id: r.id,
    entityType: r.entityType,
    title: str(f.title) ?? str(f.handle) ?? r.id,
    artist: str(f.artist),
    genre: str(f.genre),
    mood: str(f.mood),
    duration: num(f.duration) || undefined,
    owner: r.owner,
    ...(r.score === undefined ? {} : { score: +r.score.toFixed(4) }),
  };
}

/** Seconds → "58:24" / "1:02:11". Local rather than imported from format.ts, which is
 *  four lines and would cost this module its zero-import property. */
export function clock(sec: number): string {
  const s = Math.max(0, Math.round(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

const totalSecs = (recs: Array<RecLike | null>) =>
  recs.reduce((n, r) => n + (r ? num(r.fields.duration) : 0), 0);

// ── the tools ───────────────────────────────────────────────────────────────

/**
 * Register this tab's verbs with the browser's agent.
 *
 * Off React on purpose, so scripts/check-webmcp.ts can drive the tools with a ten-line
 * fake `mc`. One AbortController for the whole set: passing its signal to every
 * registerTool makes `ctl.abort()` the single unregister.
 *
 * @returns the unregister function.
 */
export function registerTools(mc: ModelContext, get: () => Deps): () => void {
  const ctl = new AbortController();
  const opts = { signal: ctl.signal };
  const reg = (t: Tool) => mc.registerTool(t, opts);

  /** Resolve a playlist by id first, then by exact name, then case-insensitively —
   *  an agent that just read `list-playlists` will quote the name back. */
  const findList = (ref: string): PlaylistLike | undefined => {
    const pls = get().playlists();
    const k = ref.trim().toLowerCase();
    return pls.find((p) => p.id === ref)
      ?? pls.find((p) => p.name === ref)
      ?? pls.find((p) => p.name.trim().toLowerCase() === k);
  };

  // ── the graph ─────────────────────────────────────────────────────────────

  reg({
    name: 'search-music',
    description:
      "Search this music graph by MEANING, across BOTH publishers at once. The query is embedded and matched inside this browser tab — it is never sent anywhere, so descriptive and vibe-based phrasing works and you do not need exact keywords: 'peak time high energy dance floor', 'rainy day melancholy piano'. Mood and genre are part of what was embedded, so energy words in the query work without a separate filter. Results carry `duration` (seconds) and `mood`, which is what lets you budget a playlist to a length and shape its energy. By default this also shows the results on the person's screen; pass show:false while you are gathering candidates so their page does not jump on every search.",
    inputSchema: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'What to find, in natural language' },
        limit: { type: 'number', description: 'Max records to return (default 20)' },
        type: { type: 'string', description: "Optional entity type: Track, Artist, Playlist, Genre, Mood or Tag. Omit for everything." },
        show: { type: 'boolean', description: 'Paint the results on the page (default true). Set false while composing.' },
      },
      required: ['query'],
    },
    async execute({ query, limit = 20, type, show = true }: ToolArgs) {
      const q = String(query ?? '').trim();
      if (!q) return text('Give me something to search for.');
      try {
        const hits = await get().search(q, Number(limit) || 20, type ? String(type) : undefined);
        if (show) get().goSearch(q);
        return text(hits.length
          ? hits.map(brief)
          : `Nothing in this snapshot matches "${q}".`);
      } catch (e) { return text(`Search failed: ${(e as Error).message}`); }
    },
  });

  reg({
    name: 'open-record',
    description:
      "Open a track, artist, genre or tag page on the person's screen. Use it to show someone what you found — it moves the page they are actually looking at, so the result is a thing they can click rather than a paragraph you wrote.",
    inputSchema: {
      type: 'object',
      properties: { id: { type: 'string', description: 'A record id, e.g. audius:track:2GROP7Z' } },
      required: ['id'],
    },
    async execute({ id }: ToolArgs) {
      const rec = await get().entity(String(id));
      if (!rec) return text(`No record ${id} in this snapshot.`);
      get().goEntity(rec.id);
      return text({ opened: brief(rec) });
    },
  });

  reg({
    name: 'describe-graph',
    description:
      'What this snapshot holds: the two publishers, how many records each contributed, and the linkset of edges that cross between them. Start here if you want to know what you are searching before you search it.',
    inputSchema: { type: 'object', properties: {} },
    async execute() {
      try { return text(await get().stats()); }
      catch (e) { return text(`Not loaded yet: ${(e as Error).message}`); }
    },
  });

  reg({
    name: 'list-relations',
    description:
      "Which typed relations a record has, and how many neighbours each leads to. `crosses: true` means at least one neighbour on that relation was published by a DIFFERENT wallet — that is the whole point of this graph: two publishers who never coordinated, joined by content addressing. Feed a relation name to `traverse` to walk it.",
    inputSchema: {
      type: 'object',
      properties: { id: { type: 'string', description: 'A record id' } },
      required: ['id'],
    },
    async execute({ id }: ToolArgs) {
      try {
        const rels = await get().relations(String(id));
        return text(rels.length ? rels : `No relations on ${id}.`);
      } catch (e) { return text(`Failed: ${(e as Error).message}`); }
    },
  });

  reg({
    name: 'traverse',
    description:
      'Walk one typed relation from a record and get the neighbours back. Use `list-relations` first to see which relations exist and which direction they run.',
    inputSchema: {
      type: 'object',
      properties: {
        id: { type: 'string', description: 'The record to walk from' },
        rel: { type: 'string', description: 'The relation name, from list-relations' },
        dir: { type: 'string', description: '"out" (default) or "in"' },
        limit: { type: 'number', description: 'Max neighbours (default 12)' },
      },
      required: ['id', 'rel'],
    },
    async execute({ id, rel, dir = 'out', limit = 12 }: ToolArgs) {
      try {
        const d = dir === 'in' ? 'in' : 'out';
        const r = await get().neighbours(String(id), String(rel), d, Number(limit) || 12);
        return text({ total: r.total, records: r.records.map(brief) });
      } catch (e) { return text(`Failed: ${(e as Error).message}`); }
    },
  });

  reg({
    name: 'browse',
    description:
      'List what is here of one kind without searching — a sample of tracks, artists, genres, moods or tags, optionally restricted to one publisher. Use it to find out what the vocabulary looks like before composing a query.',
    inputSchema: {
      type: 'object',
      properties: {
        type: { type: 'string', description: 'Track, Artist, Playlist, Genre, Mood or Tag' },
        limit: { type: 'number', description: 'Max records (default 20)' },
        owner: { type: 'string', description: 'Optional publisher wallet, from describe-graph' },
      },
      required: ['type'],
    },
    async execute({ type, limit = 20, owner }: ToolArgs) {
      try {
        const recs = await get().sample(String(type), Number(limit) || 20, owner ? String(owner) : undefined);
        return text(recs.length ? recs.map(brief) : `Nothing of type ${type}.`);
      } catch (e) { return text(`Failed: ${(e as Error).message}`); }
    },
  });

  // ── the player the person is listening to ─────────────────────────────────

  reg({
    name: 'player-state',
    description: 'What is playing right now and where the playhead is.',
    inputSchema: { type: 'object', properties: {} },
    async execute() {
      const p = get().player();
      return text({
        playing: p.playing,
        at: +p.time.toFixed(1),
        of: +p.duration.toFixed(1),
        position: clock(p.time),
        track: p.current ? brief(p.current) : null,
      });
    },
  });

  reg({
    name: 'control-player',
    description:
      "Drive the audio the person is already listening to — the same element their transport bar controls, not a copy. `play` with an id starts that track; `play` with no id resumes. Seek takes seconds.",
    inputSchema: {
      type: 'object',
      properties: {
        action: { type: 'string', description: 'play, pause, next, prev or seek' },
        id: { type: 'string', description: 'For play: the track to start' },
        seconds: { type: 'number', description: 'For seek: position in seconds' },
      },
      required: ['action'],
    },
    async execute({ action, id, seconds }: ToolArgs) {
      const p = get().player();
      switch (String(action)) {
        case 'play': {
          if (!id) { p.toggle(); return text({ playing: true }); }
          const rec = await get().entity(String(id));
          if (!rec) return text(`No record ${id} in this snapshot.`);
          if (rec.entityType !== 'Track') return text(`${rec.id} is a ${rec.entityType}, not a track.`);
          p.play(rec);
          return text({ playing: brief(rec) });
        }
        case 'pause': p.toggle(); return text({ paused: true });
        case 'next': p.next(); return text({ skipped: 'forward' });
        case 'prev': p.prev(); return text({ skipped: 'back' });
        case 'seek': {
          const t = Number(seconds);
          if (!Number.isFinite(t)) return text('seek needs `seconds`.');
          p.seek(t);
          return text({ at: clock(t) });
        }
        default: return text(`Unknown action "${action}". Use play, pause, next, prev or seek.`);
      }
    },
  });

  // ── the part that exists in this tab and nowhere else ─────────────────────

  reg({
    name: 'read-taste',
    description:
      "This session's listening model, in words: the genres, moods and artists it is heading toward, how fast it is moving, and what it is avoiding. It is derived in this browser from what the person played, skipped and rated. It is never uploaded and there is no account attached to it, so there is no server you could have asked for this.",
    inputSchema: { type: 'object', properties: {} },
    async execute() {
      try { return text(await get().taste()); }
      catch (e) { return text(`No kernel yet: ${(e as Error).message}`); }
    },
  });

  reg({
    name: 'recommend',
    description:
      'What the session model would play next, given everything it has learned in this tab. Different from search: this answers "what now", not "what matches".',
    inputSchema: {
      type: 'object',
      properties: { limit: { type: 'number', description: 'How many (default 12)' } },
    },
    async execute({ limit = 12 }: ToolArgs = {}) {
      try {
        const recs = await get().recommend(Number(limit) || 12);
        return text(recs.map(brief));
      } catch (e) { return text(`Failed: ${(e as Error).message}`); }
    },
  });

  // ── the person's library — the only tools that write ──────────────────────

  reg({
    name: 'list-playlists',
    description:
      "The person's own playlists, saved in this browser. These are theirs, and are not the graph's published Playlist records — search for those with search-music.",
    inputSchema: { type: 'object', properties: {} },
    async execute() {
      const pls = get().playlists();
      if (!pls.length) return text('No playlists yet.');
      const byId = new Map<string, RecLike | null>();
      const ids = [...new Set(pls.flatMap((p) => p.trackIds))];
      try {
        const recs = await get().entities(ids);
        ids.forEach((id, i) => byId.set(id, recs[i] ?? null));
      } catch { /* names are nice; the list is the answer */ }
      return text(pls.map((p) => {
        const recs = p.trackIds.map((t) => byId.get(t) ?? null);
        const secs = totalSecs(recs);
        return {
          id: p.id,
          name: p.name,
          tracks: p.trackIds.length,
          duration: secs || undefined,
          runningTime: secs ? clock(secs) : undefined,
        };
      }));
    },
  });

  reg({
    name: 'create-playlist',
    description:
      "Make a NEW playlist from an ordered list of track ids and save it to this browser. The order you give is the play order, so this is where you express an arc — put the high-energy picks first and the wind-down last, and it will play that way. Returns the running time so you can check it against a brief like \"about an hour\", plus a link that carries the whole playlist for the person to share. Nothing existing is touched, so this does not interrupt the person to ask. Use search-music to gather candidates (its results carry `duration` in seconds) and add up the durations yourself before calling this.",
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string', description: 'What to call it' },
        trackIds: {
          type: 'array', items: { type: 'string' },
          description: 'Track ids IN PLAY ORDER, e.g. ["audius:track:2GROP7Z", …]',
        },
      },
      required: ['name', 'trackIds'],
    },
    async execute({ name, trackIds }: ToolArgs) {
      const nm = String(name ?? '').trim();
      const ids = (Array.isArray(trackIds) ? trackIds : [])
        .filter((t): t is string => typeof t === 'string' && !!t);
      if (!nm) return text('A playlist needs a name.');
      if (!ids.length) return text('Give me at least one track id — see search-music.');

      const recs = await get().entities(ids).catch(() => ids.map(() => null));
      const missing = ids.filter((_, i) => !recs[i]);
      const kept = ids.filter((_, i) => !!recs[i]);
      if (!kept.length) return text(`None of those ${ids.length} ids are in this snapshot. Nothing was saved.`);

      const pl = get().createPlaylist(nm, kept);
      const secs = totalSecs(recs.filter(Boolean));
      get().goPlaylists(pl.id);
      return text({
        created: nm,
        tracks: kept.length,
        duration: secs,
        runningTime: clock(secs),
        // The link IS the playlist — it rides in the URL fragment, nothing was uploaded.
        link: get().shareUrl([pl]),
        ...(missing.length ? { skipped: missing } : {}),
      });
    },
  });

  reg({
    name: 'add-to-playlist',
    description:
      "Add tracks to a playlist the person ALREADY made. Because this changes something of theirs, it asks them first — a card appears and this waits for the press. If they dismiss it or do not answer, nothing is added. To make a new playlist instead, use create-playlist, which does not interrupt them.",
    inputSchema: {
      type: 'object',
      properties: {
        playlist: { type: 'string', description: 'The playlist id or name, from list-playlists' },
        trackIds: { type: 'array', items: { type: 'string' }, description: 'Track ids to append' },
      },
      required: ['playlist', 'trackIds'],
    },
    async execute({ playlist, trackIds }: ToolArgs) {
      const pl = findList(String(playlist ?? ''));
      if (!pl) return text(`No playlist called "${playlist}". Use list-playlists to see them.`);
      const ids = (Array.isArray(trackIds) ? trackIds : [])
        .filter((t): t is string => typeof t === 'string' && !!t);
      const fresh = ids.filter((t) => !pl.trackIds.includes(t));
      if (!fresh.length) return text(`Nothing new to add to "${pl.name}".`);

      const recs = await get().entities(fresh).catch(() => fresh.map(() => null));
      // Pairs, not two parallel arrays: `kept` is a subset of `fresh`, so indexing
      // `recs` by a position in `kept` would name a different track than it added.
      const found = fresh.map((id, i) => ({ id, rec: recs[i] })).filter((x) => !!x.rec);
      const kept = found.map((x) => x.id);
      if (!kept.length) return text('None of those ids are in this snapshot. Nothing was added.');

      const names = found.slice(0, 3)
        .map((x) => str(x.rec?.fields.title) ?? x.id).join(', ');
      const ok = await get().confirm(
        `Add ${kept.length} track${kept.length === 1 ? '' : 's'} to "${pl.name}"?`,
        kept.length > 3 ? `${names} and ${kept.length - 3} more` : names,
      );
      if (!ok) return text(`Not added — the person did not accept. "${pl.name}" is unchanged.`);

      const { added } = get().addToPlaylist(pl.id, kept);
      get().goPlaylists(pl.id);
      return text({ playlist: pl.name, added });
    },
  });

  reg({
    name: 'share-playlist',
    description:
      "A link that carries a playlist. It encodes the name and track ids into the URL fragment, so nothing is uploaded to produce it and no server sees it — but the link IS the data, and anyone holding it can read the playlist.",
    inputSchema: {
      type: 'object',
      properties: { playlist: { type: 'string', description: 'The playlist id or name' } },
      required: ['playlist'],
    },
    async execute({ playlist }: ToolArgs) {
      const pl = findList(String(playlist ?? ''));
      if (!pl) return text(`No playlist called "${playlist}".`);
      return text({ playlist: pl.name, tracks: pl.trackIds.length, link: get().shareUrl([pl]) });
    },
  });

  return () => ctl.abort();
}

/** The registry, in order. Exported so the check can assert it without a browser and
 *  so About.tsx has one source for the list it prints. */
export const TOOL_NAMES = [
  'search-music', 'open-record', 'describe-graph', 'list-relations', 'traverse', 'browse',
  'player-state', 'control-player',
  'read-taste', 'recommend',
  'list-playlists', 'create-playlist', 'add-to-playlist', 'share-playlist',
] as const;
