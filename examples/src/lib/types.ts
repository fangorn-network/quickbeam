// Core domain types for the schema browser.

export const ENTITY_TYPES = [
  'Artist',
  'Recording',
  'Release',
  'ReleaseGroup',
  'Work',
  'Place',
  'Event',
  'Area',
  'Instrument',
] as const;

export type EntityType = (typeof ENTITY_TYPES)[number];

export function isEntityType(v: string): v is EntityType {
  return (ENTITY_TYPES as readonly string[]).includes(v);
}

// A single Qdrant point payload. fields is loosely typed because the set
// of fields varies per entity type; scalar fields are strings/numbers/bools,
// and projection fields are arrays.
export interface EntityFields {
  title?: string;
  text?: string;
  mbid?: string;
  tags?: string;
  disambiguation?: string;
  byArtist?: string;
  area?: string;
  rating?: number;
  [key: string]: string | number | boolean | unknown[] | undefined;
}

export interface EntityPayload {
  id?: string; // mbid uuid (NOT the qdrant point id)
  entityType?: EntityType | string;
  owner?: string;
  meta?: { manifestCid?: string; [k: string]: unknown };
  fields?: EntityFields;
  [key: string]: unknown;
}

// A Qdrant point as returned by scroll/retrieve/recommend.
export interface QdrantPoint {
  id: string | number; // the Qdrant point id — used for routing
  payload?: EntityPayload;
  score?: number;
  vector?: number[] | null;
}

// Flattened summary used by cards, rails, palette.
export interface EntitySummary {
  pointId: string;
  entityType: EntityType | string;
  title: string;
  mbid?: string;
  fields: EntityFields;
  score?: number;
}

// A page reference for the breadcrumb/back-stack and Recent list.
export interface PageRef {
  kind: 'entity' | 'search' | 'browse';
  pointId?: string;
  entityType?: EntityType | string;
  label: string;
  query?: string;
  href: string;
}

// ---- Schema JSON shapes ----
export interface FieldSchemaDef {
  '@type'?: string;
  [k: string]: unknown;
}

export interface TypeSchema {
  name: string;
  definition: Record<string, FieldSchemaDef>;
}

export interface EdgeDef {
  rel: string;
  from: string;
  to: string;
  min?: number;
}

export interface CreativeCoreBundle {
  name: string;
  kind: string;
  bundle: {
    nodes: Record<string, string>;
    edges: EdgeDef[];
  };
}
