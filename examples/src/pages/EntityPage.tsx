import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Breadcrumb from '../components/Breadcrumb';
import EntityBadge from '../components/EntityBadge';
import StatusBadge from '../components/StatusBadge';
import FieldTable from '../components/FieldTable';
import RelatedRail from '../components/RelatedRail';
import SemanticNeighborGrid from '../components/SemanticNeighborGrid';
import JsonDrawer from '../components/JsonDrawer';
import SkeletonBlock from '../components/SkeletonBlock';
import { useAsync } from '../hooks/useAsync';
import { getPoint, recommend, toSummary, QdrantError } from '../lib/qdrant';
import { loadTypeSchema, loadBundle, edgesForType } from '../lib/schemas';
import type {
  CreativeCoreBundle,
  EntitySummary,
  EntityType,
  PageRef,
  TypeSchema,
} from '../lib/types';
import { isEntityType } from '../lib/types';
import { metaFor, accentColor } from '../lib/entityMeta';
import { mbUrl, formatActive, LIST_FIELDS, humanise } from '../lib/labels';
import { COPY } from '../lib/copy';
import { entityHref, entityPageRef, searchHref, searchPageRef, browseHref } from '../lib/nav';
import { relHeading } from '../lib/relationships';
import styles from './EntityPage.module.css';

interface Props {
  pointId: string;
  onVisit: (p: PageRef) => void;
}

export default function EntityPage({ pointId, onVisit }: Props) {
  const navigate = useNavigate();
  const [showJson, setShowJson] = useState(false);

  const point = useAsync(() => getPoint(pointId), [pointId]);

  const summary: EntitySummary | null = point.data ? toSummary(point.data) : null;
  const entityType = summary?.entityType ?? '';
  const typedEntityType: EntityType | null = isEntityType(entityType) ? entityType : null;

  // Record visit in back stack once loaded.
  useEffect(() => {
    if (summary) onVisit(entityPageRef(summary));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [summary?.pointId]);

  // Load schema + bundle for typed fields and connection vocabulary.
  const schema = useAsync<TypeSchema | null>(
    () => (typedEntityType ? loadTypeSchema(typedEntityType) : Promise.resolve(null)),
    [typedEntityType],
  );

  const bundle = useAsync(() => loadBundle(), []);

  // Semantic neighbors (independent async load).
  const neighbors = useAsync<EntitySummary[]>(
    () => (point.data ? recommend(pointId, 12).then((ps) => ps.map(toSummary)) : Promise.resolve([])),
    [pointId, point.data?.id],
  );

  const meta = metaFor(entityType);

  // ---- loading / error states ----
  if (point.loading) {
    return (
      <div className={styles.page}>
        <div className={styles.loadingText}>{COPY.states.loadingEntity}</div>
        <SkeletonBlock height="6rem" />
        <div style={{ height: 16 }} />
        <SkeletonBlock height="12rem" />
      </div>
    );
  }

  if (point.error) {
    const notFound = point.error instanceof QdrantError && point.error.kind === 'notfound';
    return (
      <div className={styles.page}>
        <Breadcrumb crumbs={[{ label: 'Browse', href: '/' }, { label: 'Not found' }]} />
        <h1 className={styles.h1}>Entity not found</h1>
        <p className={styles.error}>
          {notFound ? COPY.states.errorNotFound : COPY.states.errorNetwork}
        </p>
        <button type="button" className={styles.link} onClick={() => navigate('/')}>
          ← Back to browse
        </button>
      </div>
    );
  }

  if (!summary || !point.data) return null;

  const f = summary.fields;
  const title = summary.title;
  const text = typeof f.text === 'string' ? f.text : null;
  const dis = typeof f.disambiguation === 'string' ? f.disambiguation : null;
  const sortName = typeof f.sortName === 'string' ? f.sortName : null;
  const active = formatActive(f.beginYear as string, f.endYear as string);
  const external = summary.mbid && typedEntityType ? mbUrl(typedEntityType, summary.mbid) : null;
  const cancelled = f.cancelled === true;

  const subtitleParts: string[] = [];
  if (typeof f.area === 'string' && f.area) subtitleParts.push(f.area);
  if (active) subtitleParts.push(active);
  const subtitle = subtitleParts.join(' · ');

  const crumbs = [
    { label: 'Browse', href: '/' },
    typedEntityType
      ? { label: meta?.plural ?? entityType, href: browseHref(typedEntityType) }
      : { label: entityType },
    { label: title },
  ];

  function onSoftLink(value: string, field: string) {
    onVisit(searchPageRef(value));
    navigate(searchHref(value));
    void field;
  }

  function openEntity(e: EntitySummary) {
    onVisit(entityPageRef(e));
    navigate(entityHref(e.pointId));
  }

  return (
    <div className={styles.page}>
      <Breadcrumb crumbs={crumbs} />

      {/* Header card */}
      <div
        className={styles.header}
        style={{ '--accent': accentColor(entityType) } as React.CSSProperties}
      >
        <div className={styles.headerTop}>
          <EntityBadge type={entityType} size="lg" />
          {external && (
            <a
              className={styles.mbLink}
              href={external}
              target="_blank"
              rel="noreferrer"
              title={COPY.link.externalTooltip}
            >
              View on MusicBrainz ↗
            </a>
          )}
        </div>
        <h1 className={styles.h1}>{title}</h1>
        {sortName && sortName !== title && (
          <div className={styles.sortName}>{sortName}</div>
        )}
        {cancelled && (
          <div className={styles.cancelled}>
            <StatusBadge variant="error" label="CANCELLED" />
          </div>
        )}
        {subtitle && <div className={styles.subtitle}>{subtitle}</div>}
        {dis && <div className={styles.dis}>({dis})</div>}
        {text ? (
          <p className={styles.lede}>{text}</p>
        ) : (
          meta && <p className={styles.ledeMuted}>{meta.singular} · {meta.definition}</p>
        )}
        {entityType === 'Instrument' && typeof f.description === 'string' && (
          <p className={styles.lede}>{f.description}</p>
        )}
      </div>

      {/* Fields */}
      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>Fields</h2>
        <FieldTable
          fields={f}
          schema={schema.data}
          onSoftLink={onSoftLink}
          onShowJson={() => setShowJson(true)}
        />
      </section>

      {/* Connections (list fields + edge vocabulary) */}
      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>{COPY.connections.heading}</h2>
        <ConnectionsBlock
          fields={f}
          entityType={entityType}
          bundle={bundle.data}
          onSearch={onSoftLink}
        />
      </section>

      {/* Semantic neighbors */}
      <SemanticNeighborGrid
        title={title}
        neighbors={neighbors.data ?? []}
        loading={neighbors.loading}
        error={!!neighbors.error}
        onItemClick={openEntity}
      />

      <JsonDrawer payload={point.data.payload ?? {}} open={showJson} onClose={() => setShowJson(false)} />
    </div>
  );
}

// Renders list-field "Connections" + an honest note about edge vocabulary.
function ConnectionsBlock({
  fields,
  entityType,
  bundle,
  onSearch,
}: {
  fields: Record<string, unknown>;
  entityType: string;
  bundle: CreativeCoreBundle | null;
  onSearch: (value: string, field: string) => void;
}) {
  const listSections = useMemo(() => {
    const out: { field: string; items: EntitySummary[] }[] = [];
    for (const field of LIST_FIELDS) {
      const v = fields[field];
      if (Array.isArray(v) && v.length) {
        const items: EntitySummary[] = v.slice(0, 5).map((raw, i) => {
          if (typeof raw === 'string') {
            return {
              pointId: `lf-${field}-${i}`,
              entityType: 'Unknown',
              title: raw,
              fields: {},
            };
          }
          const obj = raw as Record<string, unknown>;
          return {
            pointId: `lf-${field}-${i}`,
            entityType: (obj.entityType as string) ?? 'Unknown',
            title: (obj.title as string) ?? (obj.name as string) ?? String(raw),
            fields: obj as never,
          };
        });
        out.push({ field, items });
      }
    }
    return out;
  }, [fields]);

  const edges = edgesForType(bundle, entityType);
  const relVocab = Array.from(
    new Set([...edges.outgoing.map((e) => e.rel), ...edges.incoming.map((e) => e.rel)]),
  );

  if (listSections.length === 0) {
    return (
      <div>
        <div className={styles.empty}>{COPY.connections.emptyForEntry}</div>
        {relVocab.length > 0 && (
          <div className={styles.vocab}>
            <span className={styles.vocabLabel}>
              This {metaFor(entityType)?.singular ?? entityType} can participate in:
            </span>{' '}
            {relVocab.map((r) => relHeading(r)).join(' · ')}
            <div className={styles.vocabNote}>
              No direct edges are stored in the payload — following a name runs a search.
            </div>
          </div>
        )}
      </div>
    );
  }

  return (
    <div>
      {listSections.map((sec) => (
        <RelatedRail
          key={sec.field}
          heading={humanise(sec.field)}
          mechanism={`via ${sec.field}[] · list field`}
          items={sec.items}
          onItemClick={(e) => onSearch(e.title, sec.field)}
        />
      ))}
      {relVocab.length > 0 && (
        <div className={styles.vocabNote}>
          Relationship vocabulary for this type: {relVocab.map((r) => relHeading(r)).join(' · ')}.
          Following an item runs a name search (no hard edges stored).
        </div>
      )}
    </div>
  );
}
