// Client for the publish service (proxied at /api by vite).
import type { DatasetView, Health, PublishedBatch, RegisteredSchema, SchemaDoc } from './types';

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((body as { error?: string }).error ?? `HTTP ${res.status}`);
  return body as T;
}

export const api = {
  health: () => req<Health>('/health'),

  listSchemas: () => req<{ schemas: RegisteredSchema[] }>('/schemas'),

  // Fetch a schema straight from the on-chain registry.
  summon: (name: string) =>
    req<{ name: string; schemaId: string; kind: string; fields: SchemaDoc['fields']; known: RegisteredSchema | null }>(
      `/schema/summon?name=${encodeURIComponent(name)}`,
    ),

  register: (doc: SchemaDoc) =>
    req<RegisteredSchema>('/schema/register', {
      method: 'POST',
      body: JSON.stringify({ name: doc.name, type: doc.type, titleField: doc.titleField, label: doc.type, fields: doc.fields }),
    }),

  publish: (schemaName: string, records: { fields: { [k: string]: string } }[]) =>
    req<PublishedBatch>('/publish', {
      method: 'POST',
      body: JSON.stringify({ schemaName, records }),
    }),

  published: () => req<{ datasets: DatasetView[]; published: PublishedBatch[] }>('/published'),
};
