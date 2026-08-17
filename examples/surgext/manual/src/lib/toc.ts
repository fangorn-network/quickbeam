// Build the table-of-contents tree purely from the `breadcrumb` field every heading
// node carries (e.g. "Technical Reference › Effects › Distortion"), ordered by the
// stored `page`. No edge file needed — the manual's structure is in the payloads.
import type { Point } from './types';

export interface TocNode {
  id: string;
  title: string;
  entityType: string;
  page: number;
  children: TocNode[];
}

const SEP = ' › ';
const num = (v: unknown): number => (typeof v === 'number' ? v : Number(v) || 0);

export function buildToc(points: Point[]): TocNode[] {
  const headings = points.filter((p) => typeof p.fields.breadcrumb === 'string' && p.fields.breadcrumb);
  // Shallowest-first so a parent node exists before its children attach.
  const sorted = [...headings].sort((a, b) => {
    const da = String(a.fields.breadcrumb).split(SEP).length;
    const db = String(b.fields.breadcrumb).split(SEP).length;
    return da - db || num(a.fields.page) - num(b.fields.page);
  });
  const roots: TocNode[] = [];
  const byCrumb = new Map<string, TocNode>();
  for (const p of sorted) {
    const crumb = String(p.fields.breadcrumb);
    const parts = crumb.split(SEP);
    const node: TocNode = {
      id: p.id,
      title: String(p.fields.title ?? parts[parts.length - 1]),
      entityType: p.entityType,
      page: num(p.fields.page),
      children: [],
    };
    byCrumb.set(crumb, node);
    if (parts.length === 1) {
      roots.push(node);
    } else {
      const parent = byCrumb.get(parts.slice(0, -1).join(SEP));
      (parent ? parent.children : roots).push(node);
    }
  }
  const sortRec = (ns: TocNode[]) => {
    ns.sort((a, b) => a.page - b.page);
    ns.forEach((n) => sortRec(n.children));
  };
  sortRec(roots);
  return roots;
}

// Reading order (depth-first) — for prev/next linear navigation.
export function flattenToc(roots: TocNode[]): TocNode[] {
  const out: TocNode[] = [];
  const rec = (n: TocNode) => { out.push(n); n.children.forEach(rec); };
  roots.forEach(rec);
  return out;
}

// The ancestor chain (root → … → self) for a heading id, or [] if not in the tree.
export function tocPath(roots: TocNode[], id: string): TocNode[] {
  let found: TocNode[] = [];
  const dfs = (n: TocNode, trail: TocNode[]): boolean => {
    const t = [...trail, n];
    if (n.id === id) { found = t; return true; }
    return n.children.some((c) => dfs(c, t));
  };
  roots.some((r) => dfs(r, []));
  return found;
}
