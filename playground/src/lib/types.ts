export type ScalarType = 'string' | 'number' | 'boolean';

// The flat schema shape the UI edits as JSON in the Schemas tab.
export interface SchemaDoc {
  name: string;
  type: string;
  titleField: string;
  fields: { [key: string]: ScalarType };
}

// A schema the server has registered (node schema + wrapping bundle).
export interface RegisteredSchema extends SchemaDoc {
  label: string;
  nodeSchemaId: string;
  bundleName: string;
  bundleSchemaId: string;
  dataset: string;
  watchCommand?: string;
}

export interface PublishedBatch {
  schemaName: string;
  type: string;
  dataset: string;
  commitCid: string;
  tree: string;
  parent: string | null;
  tip: string;
  txHash: string;
  entryCount: number;
  uploadedCount: number;
  reusedCount: number;
  count: number;
  records: { id: string; fields: { [k: string]: string } }[];
  at: string;
}

export interface DatasetView {
  dataset: string;
  schemaName: string;
  type: string;
  head: string | null;
  count: number;
  records: { id: string; fields: { [k: string]: string } }[];
}

export interface Health {
  ok: boolean;
  configured: boolean;
  owner: string | null;
  chain: string;
  collection: string;
}

export interface SearchHit {
  id: string | number;
  score: number;
  payload: { [k: string]: unknown };
}
