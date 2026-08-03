export interface Fields {
  entityType?: string;
  title?: string;
  text?: string;
  category?: string;
  breadcrumb?: string;
  page?: number;
  range?: string;
  parent?: string;
  author?: string;
  images?: Array<{ file: string; w?: number; h?: number; kind?: string; caption?: string; cid?: string }>;
  [k: string]: unknown;
}

export interface Point {
  id: string;
  entityType: string;
  fields: Fields;
  vector: number[];
  norm: number;
}

export interface Hit extends Point {
  score: number;
}
