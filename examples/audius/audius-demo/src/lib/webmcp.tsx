// The browser half of WebMCP. The tools live in webmcp.ts (pure, node-importable);
// this file owns the three things they cannot: React, `document`, and the consent card.
// Same split playlists.ts / playlists.tsx uses, for the same reason.
import { useCallback, useEffect, useRef, useSyncExternalStore } from 'react';
import {
  entities, entity, kernelRecommend, kernelSnapshot, neighbours, ready, relations,
  sample, search,
} from './client';
import { useKernel } from './kernel';
import { usePlayer } from './player';
import { serialize, shareUrl, usePlaylists, type Playlist } from './playlists.tsx';
import { goEntity, goPlaylists, goSearch } from './router';
import { registerTools, type Deps } from './webmcp.ts';

export { TOOL_NAMES } from './webmcp.ts';

// ── the consent card ────────────────────────────────────────────────────────
//
// sond3r puts this in a bus module (src/ui/intent.js) because it has nineteen tools
// and two directions of traffic. Here it is one promise and one <dialog>, so it stays
// beside the hook that needs it.
//
// The rule it exists to enforce: DISMISSAL AND TIMEOUT BOTH RESOLVE FALSE. An agent
// asking to change something of the person's gets a yes only from a press. Silence is
// never consent, which is why there is no "default to allow" path anywhere below.

interface Ask { id: number; question: string; detail: string; resolve: (ok: boolean) => void }

let current: Ask | null = null;
let seq = 0;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l); }; };
const snapshot = () => current;

/** Resolves false if another ask arrives first, or if the card is dismissed. One card
 *  at a time: a queue would let an agent stack modals on someone. */
function confirmWithPerson(question: string, detail: string): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    current?.resolve(false);
    current = { id: ++seq, question, detail, resolve };
    emit();
  });
}

function settle(ok: boolean) {
  const a = current;
  current = null;
  emit();
  a?.resolve(ok);
}

function ConsentCard() {
  const ask = useSyncExternalStore(subscribe, snapshot, snapshot);
  const dlg = useRef<HTMLDialogElement | null>(null);

  // Imperative, like the playlist picker: `open={…}` renders a NON-modal dialog with
  // no backdrop, no focus trap and no top layer — and the top layer is what clears the
  // fixed now-playing bar without inventing a z-index.
  useEffect(() => {
    const d = dlg.current;
    if (!d) return;
    if (!ask) { if (d.open) d.close(); return; }
    // StrictMode double-invokes, and showModal() on an open dialog throws.
    if (!d.open) d.showModal();
    // Covers Esc and the backdrop alike — every close that is not the Add button is a no.
    const onClose = () => settle(false);
    d.addEventListener('close', onClose);
    return () => d.removeEventListener('close', onClose);
  }, [ask]);

  return (
    <dialog className="dlg" ref={dlg} aria-labelledby="ask-title">
      {ask && (
        <div className="dlg-body">
          <p className="ask-who">An agent in this browser is asking</p>
          <h2 id="ask-title">{ask.question}</h2>
          {ask.detail && <p className="dlg-sub">{ask.detail}</p>}
          <div className="ask-row">
            {/* method="dialog" fires `close`, which settles false — so No, Esc and the
                backdrop are one path and cannot drift apart. */}
            <form method="dialog"><button className="playall">No</button></form>
            <button className="playall ask-yes" onClick={() => { dlg.current?.close(); settle(true); }}>
              Add
            </button>
          </div>
        </div>
      )}
    </dialog>
  );
}

// ── the hook ────────────────────────────────────────────────────────────────

/**
 * Register this tab's verbs for as long as the component is mounted.
 *
 * ONCE, on mount, reading the world through a ref that every render refreshes. The
 * obvious version — an effect with the context in its deps — re-registers on every
 * render, and the deps object is new each time: the agent then watches fourteen tools
 * vanish and reappear continuously, and any call in flight dies with the abort as "the
 * operation failed for an unknown transient reason".
 */
function useModelContext(deps: Deps) {
  const live = useRef(deps);
  live.current = deps;
  useEffect(() => {
    // No agent in this browser — nothing to register, and nothing else changes.
    if (!document.modelContext?.registerTool) return;
    return registerTools(document.modelContext, () => live.current);
  }, []);
}

/**
 * Mount point: builds the deps object from the three contexts and the client, hands it
 * to the hook, and renders the consent card.
 *
 * A component rather than a bare hook so the card has somewhere to live. It renders
 * nothing when no agent is present.
 */
export function AgentTools() {
  const player = usePlayer();
  const { refresh } = useKernel();
  const { playlists, importJson } = usePlaylists();

  // Read per call, never captured — see useModelContext.
  const live = useRef({ player, playlists, importJson, refresh });
  live.current = { player, playlists, importJson, refresh };

  /** crypto.randomUUID is undefined outside a secure context — the same bare-LAN-IP
   *  case playlists.tsx works around. */
  const uuid = useCallback(() => crypto.randomUUID?.()
    ?? `pl-${Date.now()}-${Math.random().toString(36).slice(2)}`, []);

  // Both writes go through importJson → the store's own merge(), which cannot rename
  // or remove anything. A brand-new id is appended whole; an existing id unions its
  // tracks, mine first. That is exactly create and add, with no new store API and no
  // second code path to keep honest.
  const write = useCallback((pl: Playlist) => live.current.importJson(serialize([pl])), []);

  const deps: Deps = {
    search, entity, entities, relations, neighbours, sample,
    stats: () => ready(),
    // The kernel readout is owned by the worker; refresh keeps the on-screen strip in
    // step, so what the agent reads and what the person sees cannot disagree.
    taste: async () => { const s = await kernelSnapshot(); live.current.refresh(s); return s; },
    recommend: (k) => kernelRecommend(k),
    player: () => live.current.player,
    playlists: () => live.current.playlists,
    createPlaylist: (name, trackIds) => {
      const pl = { id: uuid(), name, createdAt: Date.now(), trackIds };
      write(pl);
      // Returned, not looked up — see Deps.createPlaylist.
      return pl;
    },
    addToPlaylist: (id, trackIds) => {
      const pl = live.current.playlists.find((p) => p.id === id);
      if (!pl) return { added: 0 };
      // importJson's own count, not a re-read of live.current — React has not
      // re-rendered yet at this point, so the ref still holds the pre-write array.
      const { tracks } = write({ ...pl, trackIds: [...pl.trackIds, ...trackIds] });
      return { added: tracks };
    },
    shareUrl,
    confirm: confirmWithPerson,
    goSearch, goEntity, goPlaylists,
  };

  useModelContext(deps);
  return <ConsentCard />;
}
