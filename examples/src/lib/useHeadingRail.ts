// useHeadingRail — glue between the like/dislike signal (trip store) and the
// session kernel. It lazily loads the in-browser corpus (only once the user has
// liked something, so cold visitors pay nothing), builds the session state, ranks
// the corpus by "where you're heading", and returns ready-to-render cards.
import { useEffect, useState } from 'react';
import { loadAtlasModel } from './atlas';
import { useTrip } from './trip';
import {
  buildSession,
  queryVector,
  rankBySession,
  entityTags,
  type Signal,
} from './sessionKernel';
import type { EntitySummary, EntityType } from './types';

const POOL = 120; // corpus candidates the kernel reweights
const RAIL = 8; // cards shown in the rail

export interface HeadingRail {
  items: { entity: EntitySummary; score: number }[];
  topTags: string[]; // strongest taste tags — the rail's "leaning toward…" subtitle
}

export function useHeadingRail(): HeadingRail | null {
  const { items: likes, dislikes } = useTrip();
  const [rail, setRail] = useState<HeadingRail | null>(null);

  // Key the recompute on the membership of both lists (order matters for recency).
  const likeKey = likes.map((l) => l.id).join(',');
  const dislikeKey = dislikes.map((d) => d.id).join(',');

  useEffect(() => {
    if (!likes.length) {
      setRail(null);
      return;
    }
    let live = true;
    loadAtlasModel()
      .then((model) => {
        if (!live) return;
        // Resolve each rated id to a kernel Signal (vector + taste tags).
        const toSignal = (id: string): Signal | null => {
          const vp = model.vectorPoint(id);
          return vp ? { vector: vp.vector, tags: entityTags(vp.type, vp.fields) } : null;
        };
        const likeSignals = likes.map((l) => toSignal(l.id)).filter((s): s is Signal => !!s);
        const dislikeSignals = dislikes.map((d) => toSignal(d.id)).filter((s): s is Signal => !!s);
        if (!likeSignals.length) {
          setRail(null);
          return;
        }

        const state = buildSession(likeSignals, dislikeSignals);
        const qv = queryVector(state);
        if (!qv) {
          setRail(null);
          return;
        }
        const exclude = new Set<string>([...likes.map((l) => l.id), ...dislikes.map((d) => d.id)]);
        const candidates = model.rankByVector(qv, POOL, exclude);
        const ranked = rankBySession(candidates, state, RAIL);
        setRail({
          topTags: state.topTags,
          items: ranked.map((c) => ({
            score: c.score,
            entity: {
              pointId: c.id,
              entityType: c.type as EntityType,
              title: c.title,
              fields: c.fields as EntitySummary['fields'],
            },
          })),
        });
      })
      .catch(() => {
        if (live) setRail(null); // qdrant mode / no in-browser vectors — rail just hides
      });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [likeKey, dislikeKey]);

  return rail;
}
