// Presentation, from the baked manifest's `presentation.types` (accent/singular/
// definition) with a deterministic hashed-accent fallback.
import { manifest } from './data';

export interface TypeMeta { type: string; accent: string; singular: string; plural: string; definition?: string; }

function hashedAccent(t: string): string {
  let h = 0;
  for (let i = 0; i < t.length; i++) h = (h * 31 + t.charCodeAt(i)) | 0;
  return `hsl(${Math.abs(h) % 360}, 55%, 52%)`;
}

export async function typeMetas(): Promise<Record<string, TypeMeta>> {
  const m = await manifest();
  const pres = m.presentation?.types ?? {};
  const out: Record<string, TypeMeta> = {};
  for (const e of m.entity_types ?? []) {
    const p = pres[e.type] ?? {};
    out[e.type] = {
      type: e.type,
      accent: p.accent ?? hashedAccent(e.type),
      singular: p.singular ?? e.type,
      plural: p.plural ?? `${e.type}s`,
      definition: p.definition,
    };
  }
  return out;
}
