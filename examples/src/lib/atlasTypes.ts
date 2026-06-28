// Shared shapes for the Atlas (semantic-map) view. Kept in their own module so the
// data sources (mock.ts / shards.ts) can expose raw vectors without importing the
// projection machinery in atlas.ts (which in turn imports them).

// One entity as the data source holds it for the Atlas: identity + the document
// vector (for neighbor cosine) + an optional *pre-baked* 2-D projection. When
// `proj` is present (served by `quickbeam cdn bake`), the Atlas uses it verbatim;
// otherwise it computes a projection client-side (PCA over `vector`).
export interface AtlasRaw {
  id: string;
  type: string;
  title: string;
  vector: number[];
  proj?: [number, number];
  // The entity's payload fields (locality / coordinates / rating / imageUrl / …).
  // The bento layout reads these for geographic grouping and card content; the
  // semantic projection ignores them.
  fields: Record<string, unknown>;
}
