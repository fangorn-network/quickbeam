import { useCorpus } from '../lib/corpus';

export default function TypeBadge({ type, size }: { type: string; size?: 'sm' }) {
  const { metas } = useCorpus();
  const m = metas[type];
  return (
    <span className={`badge ${size ?? ''}`} style={{ background: m?.accent ?? '#888' }}>
      {m?.singular ?? type}
    </span>
  );
}
