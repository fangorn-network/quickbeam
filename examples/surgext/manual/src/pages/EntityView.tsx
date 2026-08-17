import { useEffect } from 'react';
import { useSearchParams, Link } from 'react-router-dom';
import { useCorpus } from '../lib/corpus';
import { useAsync } from '../lib/useAsync';
import { neighbors } from '../lib/edges';
import { recommend } from '../lib/data';
import { tocPath } from '../lib/toc';
import { useImages } from '../lib/images';
import { IPFS_GATEWAY } from '../lib/config';
import { bodyText, entityHref } from '../lib/util';
import TypeBadge from '../components/TypeBadge';
import Rail from '../components/Rail';
import ParamTable from '../components/ParamTable';
import PrevNext from '../components/PrevNext';

// entityType → the incoming patch-usage relation ("Heard in N patches").
const PATCH_IN: Record<string, string> = {
  Effect: 'usesEffect', OscillatorType: 'usesOscillator', FilterType: 'usesFilter',
};
const EMPTY = { points: [], total: 0 };

export default function EntityView() {
  const [sp] = useSearchParams();
  const id = sp.get('id') ?? '';
  const { byId, toc, flat, metas } = useCorpus();
  const images = useImages(); // file → in-memory blob: URL (one bundle, no per-figure fetch)
  const point = byId.get(id);
  const type = point?.entityType ?? '';

  useEffect(() => { document.querySelector('.main')?.scrollTo(0, 0); window.scrollTo(0, 0); }, [id]);

  const params = useAsync(() => (point ? neighbors(id, 'hasParameter', 'out') : Promise.resolve(EMPTY)), [id]);
  const seeAlso = useAsync(() => (point ? neighbors(id, 'seeAlso', 'out') : Promise.resolve(EMPTY)), [id]);
  const heardIn = useAsync(
    () => (point && PATCH_IN[type] ? neighbors(id, PATCH_IN[type], 'in', 15) : Promise.resolve(EMPTY)),
    [id, type],
  );
  const documentedIn = useAsync(
    () => (point && type === 'Parameter' ? neighbors(id, 'hasParameter', 'in') : Promise.resolve(EMPTY)),
    [id, type],
  );
  const patchUses = useAsync(
    () => (point && type === 'Patch'
      ? Promise.all([
          neighbors(id, 'usesFilter', 'out'),
          neighbors(id, 'usesOscillator', 'out'),
          neighbors(id, 'usesEffect', 'out'),
        ])
      : Promise.resolve(null)),
    [id, type],
  );
  const related = useAsync(() => (point ? recommend(id, 6) : Promise.resolve([])), [id]);

  if (!point) return <div className="reading"><p className="muted">Not found: {id}</p></div>;

  const path = tocPath(toc, id);
  const node = path[path.length - 1];
  const children = node?.children ?? [];
  const body = bodyText(point);
  const def = metas[type]?.definition;
  const f = point.fields;

  return (
    <div className="reading">
      {path.length > 1 && (
        <div className="crumbs">
          {path.slice(0, -1).map((a, i) => (
            <span key={a.id}>
              {i > 0 && <span className="sep"> › </span>}
              <Link to={entityHref(a.id)}>{a.title}</Link>
            </span>
          ))}
        </div>
      )}

      <div className="kicker"><TypeBadge type={type} /></div>
      <h1 className="title">{String(f.title ?? id)}</h1>

      {type === 'Patch' && (
        <p className="muted">
          {[f.category && `${f.category} patch`, f.author && `by ${f.author}`].filter(Boolean).join(' · ')}
        </p>
      )}
      {type !== 'Patch' && def && <p className="muted" style={{ marginTop: -4 }}>{def}</p>}

      {body && (
        <div className="prose">
          {body.split(/\n+/).map((para, i) => <p key={i}>{para}</p>)}
        </div>
      )}

      {Array.isArray(f.images) && f.images.length > 0 && (
        <div className="figures">
          {f.images.map((im, i) => {
            // Prefer the private same-origin bundle; fall back to the on-chain CID via an
            // IPFS gateway (for a deploy that reads the graph from Fangorn, no bundle).
            const bundled = images?.get(im.file);
            const src = bundled ?? (im.cid ? `${IPFS_GATEWAY}/ipfs/${im.cid}` : undefined);
            return (
              <figure key={im.file + i}>
                {src
                  ? <img src={src} alt={im.caption ?? String(f.title ?? 'figure')} />
                  : <div className="fig-loading">loading figure…</div>}
                {im.caption && <figcaption>{im.caption}</figcaption>}
              </figure>
            );
          })}
        </div>
      )}

      {children.length > 0 && (
        <div className="section-block">
          <h2>In this section</h2>
          <div className="chips">
            {children.map((c) => (
              <Link key={c.id} className="chip" to={entityHref(c.id)}>
                <TypeBadge type={c.entityType} size="sm" />
                <span className="ctitle">{c.title}</span>
              </Link>
            ))}
          </div>
        </div>
      )}

      {params.data && params.data.points.length > 0 && (
        <div className="section-block">
          <h2>Parameters</h2>
          <ParamTable params={params.data.points} />
        </div>
      )}

      {patchUses.data && (
        <>
          <Rail title="Filters" points={patchUses.data[0].points} mechanism="usesFilter" />
          <Rail title="Oscillators" points={patchUses.data[1].points} mechanism="usesOscillator" />
          <Rail title="Effects" points={patchUses.data[2].points} mechanism="usesEffect" />
        </>
      )}

      <Rail
        title="Heard in patches"
        points={heardIn.data?.points ?? []}
        total={heardIn.data?.total}
        mechanism={PATCH_IN[type]}
      />
      <Rail title="See also" points={seeAlso.data?.points ?? []} mechanism="seeAlso" />
      {documentedIn.data && documentedIn.data.points.length > 0 && (
        <Rail title="Documented in" points={documentedIn.data.points} mechanism="hasParameter" />
      )}
      <Rail
        title="Related"
        points={(related.data ?? []).filter((p) => p.id !== id)}
        mechanism="semantic similarity"
      />

      {node && <PrevNext flat={flat} id={id} />}
    </div>
  );
}
