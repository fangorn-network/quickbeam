// Atlas — a bento map of the corpus. Shelves group entities geographically (by
// coordinate, labeled with the cleaned city) or semantically (by vibe); within a
// shelf, tiles are ordered by meaning (UMAP adjacency) and sized by prominence.
// Type a query and a "Top matches" shelf flies to the top, semantically ranked.
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useDomain } from '../lib/domainContext';
import { loadAtlasModel, type AtlasModel } from '../lib/atlas';
import { buildShelves, topMatchesShelf, type BentoMode, type Shelf, type Tile } from '../lib/bento';
import { entityHref } from '../lib/nav';
import styles from './Atlas.module.css';

export default function Atlas() {
  const navigate = useNavigate();
  const [model, setModel] = useState<AtlasModel | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<BentoMode>('place');
  const [q, setQ] = useState('');
  const [busy, setBusy] = useState(false);
  const [topShelf, setTopShelf] = useState<Shelf | null>(null);
  const [highlight, setHighlight] = useState<Set<string> | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadAtlasModel()
      .then((m) => !cancelled && setModel(m))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
  }, []);

  const shelves = useMemo(() => (model ? buildShelves(model.points, mode) : []), [model, mode]);

  async function runQuery(text: string) {
    if (!model || !text.trim()) return;
    setBusy(true);
    try {
      const { neighbors } = await model.query(text.trim(), 16);
      setTopShelf(topMatchesShelf(model.points, neighbors));
      setHighlight(new Set(neighbors.map((n) => n.id)));
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } finally {
      setBusy(false);
    }
  }
  function clearQuery() {
    setQ('');
    setTopShelf(null);
    setHighlight(null);
  }

  if (error) {
    return (
      <div className={styles.empty}>
        <h2>The Atlas isn’t available here</h2>
        <p>{error}</p>
        <p className={styles.dim}>
          Run the app in <code>mock</code> (default) or <code>shards</code> mode.
        </p>
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div className={styles.headTop}>
          <div>
            <h1 className={styles.title}>Atlas</h1>
            <p className={styles.sub}>
              {model ? (
                <>
                  {model.points.length.toLocaleString()} places ·{' '}
                  {mode === 'place' ? 'grouped by area' : 'grouped by vibe'} ·{' '}
                  <span className={styles.dim}>
                    ordered by meaning ({model.projection === 'umap' ? 'UMAP' : 'PCA'})
                  </span>
                </>
              ) : (
                'Projecting the corpus…'
              )}
            </p>
          </div>
          <div className={styles.modeToggle} role="tablist" aria-label="Grouping">
            <button
              role="tab"
              aria-selected={mode === 'place'}
              className={mode === 'place' ? styles.modeOn : styles.modeOff}
              onClick={() => setMode('place')}
            >
              By area
            </button>
            <button
              role="tab"
              aria-selected={mode === 'vibe'}
              className={mode === 'vibe' ? styles.modeOn : styles.modeOff}
              onClick={() => setMode('vibe')}
            >
              By vibe
            </button>
          </div>
        </div>

        <form
          className={styles.searchRow}
          onSubmit={(e) => {
            e.preventDefault();
            void runQuery(q);
          }}
        >
          <input
            className={styles.input}
            placeholder="Find by meaning… e.g. “cozy spot to read”"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <button type="submit" className={styles.go} disabled={busy || !model}>
            {busy ? '…' : 'Search'}
          </button>
          {topShelf && (
            <button type="button" className={styles.clear} onClick={clearQuery}>
              Clear
            </button>
          )}
        </form>
      </header>

      {topShelf && <ShelfBlock shelf={topShelf} highlight={null} accent onOpen={(id) => navigate(entityHref(id))} />}
      {shelves.map((s) => (
        <ShelfBlock key={s.key} shelf={s} highlight={highlight} onOpen={(id) => navigate(entityHref(id))} />
      ))}
    </div>
  );
}

function ShelfBlock({
  shelf,
  highlight,
  accent,
  onOpen,
}: {
  shelf: Shelf;
  highlight: Set<string> | null;
  accent?: boolean;
  onOpen: (id: string) => void;
}) {
  return (
    <section className={styles.shelf}>
      <div className={styles.shelfHead}>
        <h2 className={`${styles.shelfTitle} ${accent ? styles.shelfTitleAccent : ''}`}>{shelf.label}</h2>
        {shelf.sublabel && <span className={styles.shelfSub}>{shelf.sublabel}</span>}
      </div>
      <div className={styles.grid}>
        {shelf.tiles.map((t) => (
          <TileCard
            key={t.point.id}
            tile={t}
            dim={!!highlight && !highlight.has(t.point.id)}
            onOpen={onOpen}
          />
        ))}
      </div>
    </section>
  );
}

function ratingStars(v: unknown): string | null {
  return typeof v === 'number' && v > 0 ? `★ ${v.toFixed(1)}` : null;
}

function TileCard({ tile, dim, onOpen }: { tile: Tile; dim: boolean; onOpen: (id: string) => void }) {
  const domain = useDomain();
  const p = tile.point;
  const f = p.fields;
  const meta = domain.typeMeta(p.type);
  const accent = meta.accent;
  const img = typeof f.imageUrl === 'string' && f.imageUrl ? f.imageUrl : null;
  const rating = ratingStars(f.rating);
  const place =
    (typeof f.locality === 'string' && f.locality) ||
    (typeof f.venueName === 'string' && f.venueName) ||
    null;
  const when = typeof f.dateLabel === 'string' ? f.dateLabel : null;

  return (
    <button
      className={`${styles.tile} ${styles[tile.size]} ${dim ? styles.tileDim : ''}`}
      style={{ ['--accent' as string]: accent }}
      onClick={() => onOpen(p.id)}
      title={p.title}
    >
      {img && tile.size !== 'sm' && (
        <span className={styles.thumb} style={{ backgroundImage: `url(${img})` }} aria-hidden />
      )}
      <span className={styles.tileBody}>
        <span className={styles.tileType} style={{ color: accent }}>
          {meta.icon} {meta.singular}
        </span>
        <span className={styles.tileTitle}>{p.title}</span>
        <span className={styles.tileMeta}>
          {rating && <span className={styles.chip}>{rating}</span>}
          {when && <span className={styles.chip}>{when}</span>}
          {place && <span className={styles.chipDim}>{place}</span>}
        </span>
      </span>
    </button>
  );
}
