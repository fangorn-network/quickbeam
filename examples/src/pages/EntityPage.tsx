import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Breadcrumb from '../components/Breadcrumb';
import EntityBadge from '../components/EntityBadge';
import FieldTable from '../components/FieldTable';
import RelatedRail from '../components/RelatedRail';
import SemanticNeighborGrid from '../components/SemanticNeighborGrid';
import JsonDrawer from '../components/JsonDrawer';
import SkeletonBlock from '../components/SkeletonBlock';
import { useAsync } from '../hooks/useAsync';
import { getPoint, recommend, toSummary, eventsForHost, businessByPlaceId, QdrantError } from '../lib/qdrant';
import { useDomain } from '../lib/domainContext';
import type { EntitySummary, PageRef } from '../lib/types';
import { humanise, parseJsonArray, parseHours, isOpenNow } from '../lib/labels';
import { COPY } from '../lib/copy';
import { entityHref, entityPageRef, searchHref, searchPageRef, browseHref, nearHref, nearPageRef } from '../lib/nav';
import styles from './EntityPage.module.css';

interface Props {
  pointId: string;
  onVisit: (p: PageRef) => void;
}

export default function EntityPage({ pointId, onVisit }: Props) {
  const navigate = useNavigate();
  const domain = useDomain();
  const [showJson, setShowJson] = useState(false);

  const point = useAsync(() => getPoint(pointId), [pointId]);

  const summary: EntitySummary | null = point.data ? toSummary(point.data) : null;
  const entityType = summary?.entityType ?? '';

  // Record visit in back stack once loaded.
  useEffect(() => {
    if (summary) onVisit(entityPageRef(summary));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [summary?.pointId]);

  // Semantic neighbors (independent async load).
  const neighbors = useAsync<EntitySummary[]>(
    () => (point.data ? recommend(pointId, 12).then((ps) => ps.map(toSummary)) : Promise.resolve([])),
    [pointId, point.data?.id],
  );

  // A Business's hosted events (navigable, time-ordered) — the bar→events link.
  const hostedEvents = useAsync<EntitySummary[]>(
    () => {
      const pf = point.data?.payload?.fields as Record<string, unknown> | undefined;
      const pid = pf?.placeId;
      return point.data && point.data.payload?.entityType === 'Business' && typeof pid === 'string'
        ? eventsForHost(pid).then((ps) => ps.map(toSummary))
        : Promise.resolve([]);
    },
    [pointId, point.data?.id],
  );

  // An Event's venue Business (so the event page links back to the bar).
  const venueHost = useAsync<EntitySummary | null>(
    () => {
      const pf = point.data?.payload?.fields as Record<string, unknown> | undefined;
      const hid = pf?.hostBusinessId;
      return point.data && point.data.payload?.entityType === 'Event' && typeof hid === 'string'
        ? businessByPlaceId(hid).then((p) => (p ? toSummary(p) : null))
        : Promise.resolve(null);
    },
    [pointId, point.data?.id],
  );

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

  const f = summary.fields as Record<string, unknown>;
  const title = summary.title;
  const meta = domain.typeMeta(entityType);
  const known = domain.hasType(entityType);
  const rm = domain.roleMap;

  const str = (k?: string | null) =>
    k && typeof f[k] === 'string' && (f[k] as string).trim() ? (f[k] as string).trim() : null;
  const num = (k?: string | null) => (k && typeof f[k] === 'number' ? (f[k] as number) : null);

  // Lede = the human-written editorial summary. We deliberately do NOT fall back
  // to the `text` role field: that's the embedding document blurb and just echoes
  // the rating / price / amenities / hours already shown structurally above.
  const editorial = str('editorialSummary');
  const address = str('address');

  // Event-flavored header (entityType === 'Event'): events are merged into the
  // bars graph from Eventbrite/Tribe and read their own fields explicitly rather
  // than through the Business-centric role map.
  const isEvent = entityType === 'Event';
  const dateLabel = str('dateLabel');
  const venueName = str('venueName');
  const eventCategory = (parseJsonArray(f.categories) ?? [])[0] ?? null;
  const isPast = f.isPast === true;
  const isCancelled = f.isCancelled === true;
  const ticketUrl = str('ticketUrl');
  const organizerName = str('organizerName');
  const summaryText = str('summary');

  // Header stats, driven by the role map (+ a couple of well-known place fields).
  const ratingField = rm.measures.find((m) => /rating/i.test(m) && !/count/i.test(m));
  const countField = rm.measures.find((m) => /count/i.test(m));
  const rating = num(ratingField);
  const ratingCount = num(countField);
  const price = str('priceLevel');
  const category = str(rm.subtitle); // primaryType
  const locality = str(rm.spatial);

  // Opening hours → parsed once, used for both the open/closed badge and the
  // dedicated Hours table below.
  const hoursField = Object.keys(f).find((k) => /hours/i.test(k) && typeof f[k] === 'string');
  const hours = hoursField ? parseHours(f[hoursField]) : null;
  const openNow = isOpenNow(hours);

  const stats: React.ReactNode[] = [];
  if (isEvent) {
    stats.push(
      <span key="when" className={isCancelled || isPast ? styles.closed : styles.open}>
        {isCancelled ? 'Cancelled' : isPast ? 'Past event' : 'Upcoming'}
      </span>,
    );
    if (dateLabel) stats.push(<span key="d" className={styles.stat}>{dateLabel}</span>);
    if (price) stats.push(<span key="p">{price}</span>);
    if (eventCategory) stats.push(<span key="c">{eventCategory}</span>);
    if (locality) stats.push(<span key="l">{locality}</span>);
  } else {
    if (openNow != null) {
      stats.push(
        <span key="o" className={openNow ? styles.open : styles.closed}>
          {openNow ? 'Open now' : 'Closed'}
        </span>,
      );
    }
    if (rating != null) stats.push(<span key="r" className={styles.stat}>★ {rating.toFixed(1)}</span>);
    if (ratingCount != null) stats.push(<span key="n">{ratingCount.toLocaleString()} reviews</span>);
    if (price) stats.push(<span key="p">{price}</span>);
    if (category) stats.push(<span key="c">{category}</span>);
    if (locality) stats.push(<span key="l">{locality}</span>);
  }

  // Amenities (stored as a JSON-encoded string) → chips.
  const amenities = parseJsonArray(f.amenities) ?? [];

  // Contact links.
  const website = str('website');
  const phone = str('phone');
  const maps = str(rm.media); // googleMapsUri
  const coordinates = str('coordinates'); // "lat,lng" → coordinate-proximity search

  // Fields promoted into the header / hours block — hidden from the details table
  // to avoid showing the same value twice.
  const promoted = [
    ratingField,
    countField,
    'priceLevel',
    rm.subtitle,
    rm.spatial,
    rm.media,
    'website',
    'phone',
    'amenities',
    'address',
    'coordinates',
    hoursField,
    // Event fields surfaced in the header / stats above.
    'dateLabel', 'startDate', 'startTime', 'endDate', 'startISO', 'timezone',
    'venueName', 'ticketUrl', 'organizerName', 'summary', 'isPast', 'isCancelled',
    'isOnline', 'priceMin', 'priceMax', 'isFree', 'imageUrl', 'source',
  ].filter((x): x is string => !!x);

  const external = summary.mbid ? domain.externalUrl(entityType, { mbid: summary.mbid }) : null;

  const crumbs = [
    { label: 'Browse', href: '/' },
    known
      ? { label: domain.pluralOf(entityType), href: browseHref(entityType) }
      : { label: entityType || 'Entry' },
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

  function openNearby() {
    if (!coordinates) return;
    onVisit(nearPageRef(coordinates, title));
    navigate(nearHref(coordinates));
  }

  return (
    <div className={styles.page}>
      <Breadcrumb crumbs={crumbs} />

      {/* Header card */}
      <div
        className={styles.header}
        style={{ '--accent': meta.accent } as React.CSSProperties}
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
              Open source ↗
            </a>
          )}
        </div>
        <h1 className={styles.h1}>{title}</h1>
        {stats.length > 0 && (
          <div className={styles.stats}>
            {stats.map((s, i) => (
              <span key={i} className={styles.statItem}>{s}</span>
            ))}
          </div>
        )}
        {isEvent && venueName && (
          <div className={styles.address}>
            {venueHost.data ? (
              <button
                type="button"
                className={styles.linkInline}
                onClick={() => openEntity(venueHost.data!)}
                title={`Open ${venueName}`}
              >
                {venueName}
              </button>
            ) : (
              venueName
            )}
            {address ? ` · ${address}` : ''}
          </div>
        )}
        {!isEvent && address && <div className={styles.address}>{address}</div>}
        {(isEvent ? summaryText : editorial) ? (
          <p className={styles.lede}>{isEvent ? summaryText : editorial}</p>
        ) : (
          meta.definition && <p className={styles.ledeMuted}>{meta.singular} · {meta.definition}</p>
        )}
        {amenities.length > 0 && (
          <div className={styles.amenities}>
            {amenities.map((a) => (
              <span key={a} className={styles.amenity}>{a}</span>
            ))}
          </div>
        )}
        {(website || phone || maps || ticketUrl || organizerName || coordinates) && (
          <div className={styles.contact}>
            {ticketUrl && (
              <a href={ticketUrl} target="_blank" rel="noreferrer" className={styles.contactLink}>
                Tickets ↗
              </a>
            )}
            {isEvent && organizerName && (
              <button
                type="button"
                className={styles.contactLink}
                onClick={() => onSoftLink(organizerName, 'organizer')}
                title={`Find more from ${organizerName}`}
              >
                Hosted by {organizerName}
              </button>
            )}
            {website && (
              <a href={website} target="_blank" rel="noreferrer" className={styles.contactLink}>
                Website ↗
              </a>
            )}
            {phone && (
              <a href={`tel:${phone.replace(/[^\d+]/g, '')}`} className={styles.contactLink}>
                {phone}
              </a>
            )}
            {maps && (
              <a href={maps} target="_blank" rel="noreferrer" className={styles.contactLink}>
                Map ↗
              </a>
            )}
            {coordinates && (
              <button
                type="button"
                className={styles.contactLink}
                onClick={openNearby}
                title={`Find places near ${coordinates}`}
              >
                ◎ Nearby ({coordinates})
              </button>
            )}
          </div>
        )}
      </div>

      {/* Events at this venue (navigable, upcoming first) */}
      {!isEvent && (hostedEvents.data?.length ?? 0) > 0 && (
        <EventsSection
          events={hostedEvents.data ?? []}
          loading={false}
          onOpen={openEntity}
        />
      )}

      {/* Hours */}
      {hours && (
        <section className={styles.section}>
          <h2 className={styles.sectionTitle}>Hours</h2>
          <table className={styles.hours}>
            <tbody>
              {hours.map((row) => (
                <tr key={row.day} className={/closed/i.test(row.hours) ? styles.hoursClosed : ''}>
                  <td className={styles.hoursDay}>{row.day}</td>
                  <td className={styles.hoursTime}>{row.hours}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {/* Details */}
      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>Details</h2>
        <FieldTable
          fields={summary.fields}
          hideFields={promoted}
          onSoftLink={onSoftLink}
          onShowJson={() => setShowJson(true)}
        />
      </section>

      {/* Connections (list fields + edge vocabulary) */}
      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>{COPY.connections.heading}</h2>
        <ConnectionsBlock fields={f} entityType={entityType} onSearch={onSoftLink} />
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

// A venue's events: time-ordered, navigable. Upcoming first (the soonest tagged
// "Next"), then a capped list of past events — so landing on a bar immediately
// shows what's coming up and where.
function EventsSection({
  events,
  loading,
  onOpen,
}: {
  events: EntitySummary[];
  loading: boolean;
  onOpen: (e: EntitySummary) => void;
}) {
  const isPast = (e: EntitySummary) => (e.fields as Record<string, unknown>).isPast === true;
  const upcoming = events.filter((e) => !isPast(e));
  const past = events.filter(isPast);
  const PAST_CAP = 8;
  return (
    <section className={styles.section}>
      <h2 className={styles.sectionTitle}>
        Events{upcoming.length > 0 ? ` · ${upcoming.length} upcoming` : ''}
      </h2>
      {loading && <div className={styles.empty}>Loading events…</div>}
      {!loading && events.length === 0 && <div className={styles.empty}>No events found.</div>}
      {upcoming.length > 0 && (
        <div className={styles.eventList}>
          {upcoming.map((e, i) => (
            <EventRow key={e.pointId} e={e} onOpen={onOpen} next={i === 0} />
          ))}
        </div>
      )}
      {past.length > 0 && (
        <>
          <div className={styles.eventGroupLabel}>Past</div>
          <div className={styles.eventList}>
            {past.slice(0, PAST_CAP).map((e) => (
              <EventRow key={e.pointId} e={e} onOpen={onOpen} />
            ))}
          </div>
          {past.length > PAST_CAP && (
            <div className={styles.empty}>+{past.length - PAST_CAP} more past events</div>
          )}
        </>
      )}
    </section>
  );
}

function EventRow({
  e,
  onOpen,
  next,
}: {
  e: EntitySummary;
  onOpen: (e: EntitySummary) => void;
  next?: boolean;
}) {
  const f = e.fields as Record<string, unknown>;
  const when = (typeof f.dateLabel === 'string' && f.dateLabel)
    || (typeof f.startDate === 'string' && f.startDate) || 'Date TBA';
  const where = (typeof f.venueName === 'string' && f.venueName)
    || (typeof f.locality === 'string' && f.locality) || '';
  const cancelled = f.isCancelled === true;
  return (
    <button type="button" className={styles.eventRow} onClick={() => onOpen(e)}>
      <span className={styles.eventWhen}>{when}</span>
      <span className={styles.eventName}>
        {e.title}
        {next && <span className={styles.eventNext}>Next</span>}
        {cancelled && <span className={styles.eventCancelled}>cancelled</span>}
      </span>
      {where && <span className={styles.eventWhere}>{where}</span>}
    </button>
  );
}

// Renders list-field "Connections" + an honest note about edge vocabulary.
function ConnectionsBlock({
  fields,
  entityType,
  onSearch,
}: {
  fields: Record<string, unknown>;
  entityType: string;
  onSearch: (value: string, field: string) => void;
}) {
  const domain = useDomain();
  const listSections = useMemo(() => {
    const out: { field: string; items: EntitySummary[] }[] = [];
    for (const field of domain.connectionFields(fields)) {
      const v = fields[field] as unknown[];
      const items: EntitySummary[] = v.slice(0, 5).map((raw, i) => {
        if (typeof raw === 'string') {
          return { pointId: `lf-${field}-${i}`, entityType: 'Unknown', title: raw, fields: {} };
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
    return out;
  }, [fields, domain]);

  const relVocab = domain.relVocabForType(entityType);

  if (listSections.length === 0) {
    return (
      <div>
        <div className={styles.empty}>{COPY.connections.emptyForEntry}</div>
        {relVocab.length > 0 && (
          <div className={styles.vocab}>
            <span className={styles.vocabLabel}>
              This {domain.typeMeta(entityType).singular} can participate in:
            </span>{' '}
            {relVocab.map((r) => humanise(r)).join(' · ')}
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
          Relationship vocabulary for this type: {relVocab.map((r) => humanise(r)).join(' · ')}.
          Following an item runs a name search (no hard edges stored).
        </div>
      )}
    </div>
  );
}
