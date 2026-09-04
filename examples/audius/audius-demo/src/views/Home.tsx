import { useEffect, useState } from 'react';
import Card from '../components/Card';
import KernelRail from '../components/KernelRail';
import SearchBar from '../components/SearchBar';
import { sample } from '../lib/client';
import { useKernel } from '../lib/kernel';
import { goSearch } from '../lib/router';
import type { Rec, Stats } from '../lib/types';

const EXAMPLES = [
  'dark acid groove house with a moody bassline',
  'uplifting soulful UK garage vocals',
  'late night driving synthwave',
  'warm analog bassline, no vocals',
];

export default function Home({ stats }: { stats: Stats }) {
  const [a, b] = stats.publishers;
  const [popular, setPopular] = useState<Rec[]>([]);
  const [independent, setIndependent] = useState<Rec[]>([]);
  const { railVersion } = useKernel();

  useEffect(() => {
    if (a) void sample('Track', 6, a.owner).then(setPopular);
    if (b) void sample('Track', 6, b.owner).then(setIndependent);
  }, [a?.owner, b?.owner]);

  // Read from the snapshot, never typed in — a hard-coded count goes stale the
  // next time anyone re-bakes, and this line is the page's only factual claim.
  const tracks = stats.publishers.reduce((n, p) => n + (p.counts.Track ?? 0), 0);

  return (
    <>
      <section className="hero">
        <h1>Search music by <em>how it sounds</em>.</h1>
        <p>
          Describe a mood, a texture, a time of night. The search reads the whole
          catalogue from inside this tab. Nothing you type is sent anywhere.
        </p>

        <SearchBar autoFocus />
        <div className="suggestions">
          {EXAMPLES.map((e) => (
            <button key={e} className="suggestion" onClick={() => goSearch(e)}>{e}</button>
          ))}
        </div>

        <p className="hero-stats">
          {tracks.toLocaleString()} tracks · {stats.records.toLocaleString()} records ·{' '}
          {stats.edges.toLocaleString()} connections · searched in this browser
        </p>
      </section>

      {/* Recommendations first once the session has any signal — the page should
          answer what you've done, not repeat what it showed on load. */}
      <KernelRail railVersion={railVersion} stats={stats} />

      {popular.length > 0 && (
        <>
          <div className="section-head">
            <h2>Popular right now</h2>
          </div>
          <div className="grid">
            {popular.map((t) => <Card key={t.id} rec={t} queue={popular} />)}
          </div>
        </>
      )}

      {/* Its own row rather than mixed in: this catalogue is a hundred tracks against
          twenty-six thousand, so a blended sample would show it roughly never. */}
      {independent.length > 0 && (
        <>
          <div className="section-head">
            <h2>Independent releases</h2>
            {b?.label && <span className="count">from {b.label}</span>}
          </div>
          <div className="grid">
            {independent.map((t) => <Card key={t.id} rec={t} queue={independent} />)}
          </div>
        </>
      )}
    </>
  );
}
