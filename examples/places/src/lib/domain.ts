// The Domain: a single source of truth that makes the browser schema-agnostic.
//
// Everything the UI used to hardcode (entity types, per-type icons/colors, field
// labels, relationship phrasing, the secondary-line summary) is now *derived* from:
//   1. an inferred `role_map` (title/subtitle/tags/temporal/spatial/…), either baked
//      into a domain manifest by `quickbeam cdn bake` or inferred live here from a
//      sample of the collection (see lib/roles.ts);
//   2. an optional presentation overlay (icons / accents / label & external-URL
//      overrides) — pure polish, with generic fallbacks when absent.
//
// Swap the manifest and the same app browses recipes, movies, OSM changesets, … with
// no code change.
import { inferRoles, fieldLabel as roleFieldLabel } from './roles';
import type { RoleMap } from './roles';
import { scroll, setCollection } from './qdrant';
import type { EntitySummary } from './types';
import { humanise } from './labels';
import { DATA_SOURCE } from './config';
import { shardManifest } from './shards';

export interface EdgeDef {
  rel: string;
  from: string;
  to: string;
  min?: number;
}

export interface TypePresentation {
  icon?: string;
  accent?: string; // any CSS color
  letter?: string;
  singular?: string;
  plural?: string;
  definition?: string;
}

export interface Presentation {
  types?: Record<string, TypePresentation>;
  fieldLabels?: Record<string, string>;
  externalUrl?: Record<string, string>; // type -> template with {mbid}/{id}
  hideConnections?: string[]; // list-field names to suppress from "Connections"
}

// The on-disk overlay (public/domain.json) and the baked CDN manifest share this
// superset of fields. Everything is optional; the loader fills the gaps.
export interface DomainManifest {
  collection?: string;
  role_map?: RoleMap;
  entity_types?: Array<{ type: string; count?: number } | string>;
  bundle?: { nodes?: Record<string, string>; edges?: EdgeDef[] };
  presentation?: Presentation;
}

export interface TypeMeta {
  type: string;
  icon: string;
  accent: string;
  letter: string;
  singular: string;
  plural: string;
  definition?: string;
}

// Deterministic, readable accent for types with no overlay color.
function hashedAccent(type: string): string {
  let h = 0;
  for (let i = 0; i < type.length; i++) h = (h * 31 + type.charCodeAt(i)) | 0;
  return `hsl(${Math.abs(h) % 360}, 60%, 62%)`;
}

function fallbackLetter(type: string): string {
  const caps = type.replace(/[^A-Z]/g, '');
  if (caps.length >= 2) return caps.slice(0, 2);
  return (type[0] ?? '?').toUpperCase();
}

export class Domain {
  readonly roleMap: RoleMap;
  readonly entityTypes: string[];
  readonly edges: EdgeDef[];
  readonly nodes: Record<string, string>;
  readonly presentation: Presentation;

  constructor(roleMap: RoleMap, entityTypes: string[], manifest: DomainManifest) {
    this.roleMap = roleMap;
    this.entityTypes = entityTypes;
    this.edges = manifest.bundle?.edges ?? [];
    this.nodes = manifest.bundle?.nodes ?? {};
    this.presentation = manifest.presentation ?? {};
  }

  hasType(t: string): boolean {
    return this.entityTypes.includes(t);
  }

  typeMeta(type: string): TypeMeta {
    const p = this.presentation.types?.[type] ?? {};
    const singular = p.singular ?? humanise(type);
    return {
      type,
      icon: p.icon ?? '◆',
      accent: p.accent ?? hashedAccent(type),
      letter: p.letter ?? fallbackLetter(type),
      singular,
      plural: p.plural ?? `${singular}s`,
      definition: p.definition,
    };
  }

  accentColor(type: string): string {
    return this.typeMeta(type).accent;
  }

  pluralOf(type: string): string {
    return this.typeMeta(type).plural;
  }

  // Human label for a payload field: overlay override > role label > humanise.
  fieldLabel(key: string): string {
    const override = this.presentation.fieldLabels?.[key];
    if (override) return override;
    return roleFieldLabel(key);
  }

  // External link template (e.g. MusicBrainz), filled with the record's mbid/id.
  externalUrl(type: string, ids: { mbid?: string; id?: string }): string | null {
    const tmpl = this.presentation.externalUrl?.[type];
    if (!tmpl) return null;
    const url = tmpl
      .replace(/\{mbid\}/g, ids.mbid ?? '')
      .replace(/\{id\}/g, ids.id ?? '');
    return url.includes('{') ? null : url;
  }

  // Fields never shown as labelled rows (rendered elsewhere as header/lede).
  isSuppressedField(key: string): boolean {
    if (key === 'entityType' || key === 'schemaVersion') return true;
    if (key === this.roleMap.title) return true;
    return this.roleMap.text.includes(key);
  }

  // String fields whose value is a name → clicking runs a search (soft link):
  // the byline (subtitle) and geographic anchor (spatial).
  isSoftLinkField(key: string): boolean {
    return key === this.roleMap.subtitle || key === this.roleMap.spatial;
  }

  isTagField(key: string): boolean {
    return this.roleMap.tags.includes(key);
  }

  // Array-valued "Connections" fields = projected neighbor lists. Any list field
  // that isn't a tag facet and isn't explicitly hidden via presentation overlay
  // (e.g. the noisy "nearby businesses" projection).
  connectionFields(fields: Record<string, unknown>): string[] {
    const hidden = new Set(this.presentation.hideConnections ?? []);
    return Object.keys(fields).filter(
      (k) =>
        Array.isArray(fields[k]) &&
        (fields[k] as unknown[]).length > 0 &&
        !this.isTagField(k) &&
        !hidden.has(k),
    );
  }

  edgesForType(type: string): { outgoing: EdgeDef[]; incoming: EdgeDef[] } {
    return {
      outgoing: this.edges.filter((e) => e.from === type),
      incoming: this.edges.filter((e) => e.to === type),
    };
  }

  relVocabForType(type: string): string[] {
    const { outgoing, incoming } = this.edgesForType(type);
    return Array.from(new Set([...outgoing.map((e) => e.rel), ...incoming.map((e) => e.rel)]));
  }

  // Role-driven one-line summary, replacing the old per-type switch. Picks the
  // byline, the most informative tag, and a date — whichever this schema has.
  secondaryLine(e: EntitySummary): string {
    const f = e.fields as Record<string, unknown>;
    const str = (v: unknown): string | null =>
      typeof v === 'string' && v.trim() ? v.trim() : null;
    // Events are time-and-place anchored: the day and venue are what matter, so
    // the card byline leads with the date then the venue/locality.
    if (e.entityType === 'Event') {
      const evParts: string[] = [];
      const when = str(f.dateLabel) ?? str(f.startDate);
      if (when) evParts.push(f.isPast === true ? `${when} (past)` : when);
      const where = str(f.venueName) ?? str(f.locality);
      if (where) evParts.push(where);
      return evParts.join(' · ');
    }
    const parts: string[] = [];
    const sub = this.roleMap.subtitle && str(f[this.roleMap.subtitle]);
    if (sub) parts.push(sub);
    const tagField = this.roleMap.tags[0];
    if (tagField) {
      const tv = f[tagField];
      const first = Array.isArray(tv) ? tv.find((x) => typeof x === 'string') : str(tv);
      if (first) parts.push(String(first));
    }
    const temporal = this.roleMap.temporal && str(f[this.roleMap.temporal]);
    if (temporal) parts.push(temporal);
    return parts.join(' · ');
  }

  // First tag field's values — used for tag pills on cards.
  primaryTags(fields: Record<string, unknown>): string[] {
    const tagField = this.roleMap.tags[0];
    if (!tagField) return [];
    const v = fields[tagField];
    if (Array.isArray(v)) return v.filter((x): x is string => typeof x === 'string');
    if (typeof v === 'string') return v.split(',').map((s) => s.trim()).filter(Boolean);
    return [];
  }
}

const DEFAULT_ROLE_MAP: RoleMap = {
  identity: 'id', title: 'title', subtitle: null, temporal: null, spatial: null,
  media: null, tags: [], measures: [], relations: [], text: [], labels: {}, fields: [],
};

function manifestTypes(m: DomainManifest): string[] {
  if (m.entity_types?.length) {
    return m.entity_types.map((e) => (typeof e === 'string' ? e : e.type));
  }
  if (m.bundle?.nodes) return Object.keys(m.bundle.nodes);
  return [];
}

// Load the active domain. In `shards` mode the CDN manifest is fully self-describing
// (role_map / entity_types / bundle / presentation baked by `cdn bake`); otherwise we
// read the optional static overlay and infer any gaps from a live sample.
export async function loadDomain(): Promise<Domain> {
  let manifest: DomainManifest = {};
  if (DATA_SOURCE === 'shards') {
    manifest = await shardManifest();
  } else {
    try {
      const res = await fetch('/domain.json');
      if (res.ok) manifest = (await res.json()) as DomainManifest;
    } catch {
      /* no overlay — fully inferred */
    }
  }

  if (manifest.collection) setCollection(manifest.collection);

  let roleMap = manifest.role_map ?? null;
  let types = manifestTypes(manifest);

  if (!roleMap || types.length === 0) {
    // Infer from a sample of the live collection (mirrors server.py at runtime).
    try {
      const { points } = await scroll({ limit: 256 });
      if (!roleMap) {
        roleMap = inferRoles(points.map((p) => (p.payload?.fields ?? {}) as Record<string, unknown>));
      }
      if (types.length === 0) {
        const seen = new Set<string>();
        for (const p of points) {
          const t = p.payload?.entityType;
          if (typeof t === 'string') seen.add(t);
        }
        types = Array.from(seen).sort();
      }
    } catch {
      /* Qdrant unreachable — fall back to defaults; the UI shows a connection badge. */
    }
  }

  roleMap = roleMap ?? { ...DEFAULT_ROLE_MAP };
  if (!roleMap.title) roleMap.title = 'title'; // the builder always writes a title

  return new Domain(roleMap, types, manifest);
}
