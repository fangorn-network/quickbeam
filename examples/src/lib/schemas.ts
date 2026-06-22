// Runtime loaders for the schema snapshots in /public/schemas.
import type { CreativeCoreBundle, EdgeDef, TypeSchema, EntityType } from './types';

let bundleCache: CreativeCoreBundle | null = null;
const typeSchemaCache: Record<string, TypeSchema | null> = {};

const TYPE_FILE: Record<EntityType, string> = {
  Area: 'fangorn.mb.area.v3.json',
  Artist: 'fangorn.mb.artist.v3.json',
  Event: 'fangorn.mb.event.v3.json',
  Instrument: 'fangorn.mb.instrument.v3.json',
  Place: 'fangorn.mb.place.v3.json',
  Recording: 'fangorn.mb.recording.v3.json',
  ReleaseGroup: 'fangorn.mb.releasegroup.v3.json',
  Release: 'fangorn.mb.release.v3.json',
  Work: 'fangorn.mb.work.v3.json',
};

export async function loadBundle(): Promise<CreativeCoreBundle | null> {
  if (bundleCache) return bundleCache;
  try {
    const res = await fetch('/schemas/fangorn.mb.creativecore.v3.json');
    if (!res.ok) return null;
    bundleCache = (await res.json()) as CreativeCoreBundle;
    return bundleCache;
  } catch {
    return null;
  }
}

export async function loadTypeSchema(type: EntityType): Promise<TypeSchema | null> {
  if (type in typeSchemaCache) return typeSchemaCache[type];
  try {
    const res = await fetch(`/schemas/${TYPE_FILE[type]}`);
    if (!res.ok) {
      typeSchemaCache[type] = null;
      return null;
    }
    const data = (await res.json()) as TypeSchema;
    typeSchemaCache[type] = data;
    return data;
  } catch {
    typeSchemaCache[type] = null;
    return null;
  }
}

// Edges this type participates in (as `from` or `to`), for the Connections vocab.
export function edgesForType(
  bundle: CreativeCoreBundle | null,
  type: string,
): { outgoing: EdgeDef[]; incoming: EdgeDef[] } {
  if (!bundle) return { outgoing: [], incoming: [] };
  const edges = bundle.bundle.edges ?? [];
  return {
    outgoing: edges.filter((e) => e.from === type),
    incoming: edges.filter((e) => e.to === type),
  };
}

// Field @type lookup for the FieldTable "Type" column.
export function fieldTypeOf(schema: TypeSchema | null, key: string): string | null {
  const def = schema?.definition?.[key];
  if (!def) return null;
  const t = def['@type'];
  return typeof t === 'string' ? t : null;
}
