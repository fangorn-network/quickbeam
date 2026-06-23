// Core domain types for the schema browser. Entity types are no longer a fixed
// enum — they are discovered per-domain (see lib/domain.ts), so `EntityType` is just
// a string alias kept for readability at call sites.

export type EntityType = string;

// A single Qdrant point payload. `fields` is loosely typed because the set of fields
// varies per entity type and per domain; scalar fields are strings/numbers/bools and
// projection fields are arrays.
export interface EntityFields {
  title?: string;
  text?: string;
  mbid?: string;
  [key: string]: string | number | boolean | unknown[] | undefined;
}

export interface EntityPayload {
  id?: string; // stable record id (e.g. mbid) — NOT the qdrant point id
  entityType?: EntityType;
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
  entityType: EntityType;
  title: string;
  mbid?: string;
  fields: EntityFields;
  score?: number;
}

// A page reference for the breadcrumb/back-stack and Recent list.
export interface PageRef {
  kind: 'entity' | 'search' | 'browse';
  pointId?: string;
  entityType?: EntityType;
  label: string;
  query?: string;
  href: string;
}
