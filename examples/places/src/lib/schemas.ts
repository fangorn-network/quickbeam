// Publishable-schema registry — the data model for the "schema browser" shell.
//
// A discovery client reads records; a *publishing* client writes them. Every write
// target is a Fangorn schema (a field map registered on-chain). This registry is the
// generic description the create UI renders from: add an entry here and the app grows
// a new "create <thing>" form + browser card with no bespoke UI. Today it holds one
// schema (BusinessProfile); it's shaped so music/audiobook/etc. drop in the same way.
//
// The field defs mirror the on-chain schema field-map (e.g. schemas/business-profile
// .json) plus the UI metadata a form needs (label, multiline, required). Encryption is
// declared here but *performed* server-side at publish time (see lib/publish.ts) so no
// key material ever touches the browser bundle.

const env = ((import.meta as { env?: Record<string, string | undefined> }).env) ?? {};

export type FieldType = 'string' | 'text' | 'url' | 'encrypted';

export interface SchemaField {
  key: string;
  label: string;
  type: FieldType; // 'text' → multiline; 'encrypted' → settled-gadget (encrypted server-side)
  required?: boolean;
  placeholder?: string;
  help?: string;
}

export interface PublishableSchema {
  /** Dot-name used to register/publish (fangorn schema register <id>). */
  id: string;
  /** On-chain schema id (hex), once registered. Empty → publish runs in draft mode. */
  schemaId: string;
  /** Root/node type — becomes each record's `entityType` at embed time. */
  rootType: string;
  title: string;
  description: string;
  icon: string;
  /** Target CDN domain the watcher appends to (for user-facing messaging only). */
  domain: string;
  fields: SchemaField[];
  /**
   * Map an existing entity's fields onto this schema's keys, so "claim this profile"
   * opens a form pre-filled from the listing the owner is claiming. Pure + optional.
   */
  prefillFrom?: (fields: Record<string, unknown>) => Record<string, string>;
}

const str = (v: unknown): string => (typeof v === 'string' ? v : v == null ? '' : String(v));

// The first schema: an owner-authored business profile. Extends schemas/place.json
// (name / description / encrypted address) with the fields the `places` role_map
// renders (title / placeId / locality / primaryType) so a claimed profile shows up
// alongside the Google listing rather than as an orphan card.
const businessProfile: PublishableSchema = {
  id: env.VITE_BUSINESSPROFILE_SCHEMA ?? 'fangorn.places.businessprofile.v0',
  schemaId: env.VITE_BUSINESSPROFILE_SCHEMA_ID ?? '',
  rootType: 'BusinessProfile',
  title: 'Business profile',
  description: 'Claim your business and publish an owner-authored profile.',
  icon: '✔️',
  domain: env.VITE_DOMAIN || 'places',
  fields: [
    { key: 'title', label: 'Business name', type: 'string', required: true,
      placeholder: "e.g. Jimmy's Tackle & Coffee" },
    { key: 'description', label: 'About', type: 'text', required: true,
      placeholder: 'What you offer, what makes you worth a visit…',
      help: 'A few sentences. This is what people search against.' },
    { key: 'category', label: 'Category', type: 'string',
      placeholder: 'e.g. Bait shop · Café' },
    { key: 'primaryType', label: 'Primary type', type: 'string',
      placeholder: 'e.g. cafe' },
    { key: 'locality', label: 'Town / locality', type: 'string',
      placeholder: 'e.g. Eagle River' },
    { key: 'url', label: 'Website', type: 'url', placeholder: 'https://…' },
    { key: 'phone', label: 'Phone', type: 'string', placeholder: '+1 …' },
    { key: 'hours', label: 'Hours', type: 'text', placeholder: 'Mon–Fri 7–5 · Sat 8–2' },
    { key: 'address', label: 'Address', type: 'encrypted',
      placeholder: 'Street address',
      help: 'Encrypted on publish (settled gadget) — released only per your terms.' },
    // Carried through so the profile binds to the listing it claims; hidden in the UI.
    { key: 'placeId', label: 'Place ID', type: 'string' },
  ],
  prefillFrom: (f) => ({
    title: str(f.title),
    description: str(f.editorialSummary ?? f.text),
    category: Array.isArray(f.categories) ? f.categories.map(str).join(' · ') : str(f.categories),
    primaryType: str(f.primaryType),
    locality: str(f.locality),
    placeId: str(f.placeId),
  }),
};

export const SCHEMAS: PublishableSchema[] = [businessProfile];

export function getSchema(id: string): PublishableSchema | undefined {
  return SCHEMAS.find((s) => s.id === id);
}

// Fields hidden from the form (carried through prefill / defaults, not user-edited).
const HIDDEN_KEYS = new Set(['placeId']);
export function visibleFields(schema: PublishableSchema): SchemaField[] {
  return schema.fields.filter((f) => !HIDDEN_KEYS.has(f.key));
}
