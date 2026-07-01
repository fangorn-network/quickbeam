// Publish seam — the write half of the schema browser.
//
// Publishing a record means: encrypt any encrypted fields, pack a one-record
// RecordSet, pin it to IPFS, and emit ManifestPublished on-chain. The IPFS pin needs
// a Pinata key and the pack is done by the fangorn TS lib — both belong server-side,
// NOT in a browser bundle. So this seam POSTs the plain fields to a publish endpoint
// (a thin node service that runs `@fangorn-network/sdk` PublisherRole) when one is
// configured via VITE_PUBLISH_URL, and otherwise degrades to an honest local *draft*
// — mirroring claims.ts, which shows a real explainer rather than faking a tx.
//
// Once the endpoint is live the loop closes with zero call-site changes: the watcher
// (quickbeam watch --cdn-dir/--cdn-domain) embeds the new ManifestPublished record and
// ships it as a CDN delta shard. See docs/FANGORN_TO_SOND3R.md.

import type { PublishableSchema } from './schemas';

const env = ((import.meta as { env?: Record<string, string | undefined> }).env) ?? {};
const PUBLISH_URL = (env.VITE_PUBLISH_URL ?? '').replace(/\/$/, '');

export interface PublishInput {
  schema: PublishableSchema;
  /** Field key → user value (strings; encryption/typing handled at the endpoint). */
  record: Record<string, string>;
  /** The signed-in publisher address (Privy embedded wallet), when available. */
  owner?: string | null;
}

export interface PublishResult {
  ok: boolean;
  /** 'onchain' when the endpoint published; 'draft' when saved locally only. */
  mode: 'onchain' | 'draft';
  message: string;
  manifestUri?: string;
  txHash?: string;
}

const DRAFT_KEY = 'fangorn.publish.drafts';

// Persist a draft so a create form isn't lost when no endpoint is configured (and so a
// future "my drafts" view / retry-on-publish has something to read).
function saveDraft(input: PublishInput): void {
  try {
    const raw = localStorage.getItem(DRAFT_KEY);
    const drafts = raw ? (JSON.parse(raw) as unknown[]) : [];
    drafts.push({
      schemaId: input.schema.id,
      rootType: input.schema.rootType,
      owner: input.owner ?? null,
      record: input.record,
      at: Date.now(),
    });
    localStorage.setItem(DRAFT_KEY, JSON.stringify(drafts));
  } catch {
    // localStorage unavailable (private mode / quota) — a draft is best-effort.
  }
}

export async function publishRecord(input: PublishInput): Promise<PublishResult> {
  // Trim empties so the endpoint / draft records only what was filled in.
  const record: Record<string, string> = {};
  for (const [k, v] of Object.entries(input.record)) {
    if (typeof v === 'string' && v.trim()) record[k] = v.trim();
  }

  if (!PUBLISH_URL || !input.schema.schemaId) {
    saveDraft({ ...input, record });
    return {
      ok: true,
      mode: 'draft',
      message:
        'Saved as a local draft. On-chain publishing isn’t configured for this build ' +
        '(set VITE_PUBLISH_URL and the schema id) — once it is, this exact record ' +
        'publishes and appears automatically via the live CDN.',
    };
  }

  try {
    const res = await fetch(`${PUBLISH_URL}/publish`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        schemaName: input.schema.id,
        schemaId: input.schema.schemaId,
        rootType: input.schema.rootType,
        owner: input.owner ?? null,
        fields: record,
      }),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => '');
      return { ok: false, mode: 'onchain', message: `Publish failed: HTTP ${res.status} ${detail}`.trim() };
    }
    const data = (await res.json()) as { manifestUri?: string; txHash?: string };
    return {
      ok: true,
      mode: 'onchain',
      message: 'Published on-chain. It’ll appear here within a minute or two as the watcher embeds it.',
      manifestUri: data.manifestUri,
      txHash: data.txHash,
    };
  } catch (e) {
    return { ok: false, mode: 'onchain', message: `Publish error: ${(e as Error).message}` };
  }
}
