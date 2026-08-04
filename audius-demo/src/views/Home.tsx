import { useEffect, useState } from 'react';
import Card from '../components/Card';
import KernelRail from '../components/KernelRail';
import RootLedger from '../components/RootLedger';
import SearchBar from '../components/SearchBar';
import { sample } from '../lib/client';
import { useKernel } from '../lib/kernel';
import { shortAddr } from '../lib/format';
import { goSearch } from '../lib/router';
import type { Rec, Stats } from '../lib/types';

const EXAMPLES = [
  'dark acid groove house with a moody bassline',
  'uplifting soulful UK garage vocals',
  'late night driving synthwave',
  'Disclosure',
];

export default function Home({ stats }: { stats: Stats }) {
  const [a, b] = stats.publishers;
  const [platformTracks, setPlatformTracks] = useState<Rec[]>([]);
  const [artistTracks, setArtistTracks] = useState<Rec[]>([]);
  const { railVersion } = useKernel();

  useEffect(() => {
    if (a) void sample('Track', 6, a.owner).then(setPlatformTracks);
    if (b) void sample('Track', 6, b.owner).then(setArtistTracks);
  }, [a?.owner, b?.owner]);

  return (
    <>
      <section className="hero">
        <div className="hero-kicker">A Fangorn demo, built on live Audius data</div>
        <h1>Two publishers. <em>One graph.</em></h1>
        <p>
          The platform published its catalogue from one wallet. The artist published
          their own from another. Neither holds the other's data — yet search and
          browsing run straight across both, in your browser, with nothing sent to a
          server.
        </p>

        <RootLedger stats={stats} />

        <SearchBar autoFocus />
        <div className="suggestions">
          {EXAMPLES.map((e) => (
            <button key={e} className="suggestion" onClick={() => goSearch(e)}>{e}</button>
          ))}
        </div>
      </section>

      <div className="banner">
        <div>
          <b>The platform's graph has a hole shaped like the artist.</b> Its records
          reference them — playlists hold their tracks, other artists remix them — but
          their catalogue lives in their own repo, under their own key. Search for the
          artist and you'll get two records back: the platform's thin reference, and
          the artist's own. A <code>sameAs</code> edge is what says they're the same
          person.
        </div>
      </div>

      {stats.converged > 0 && (
        <div className="banner">
          <div>
            <b>{stats.converged} vertices merged without anyone arranging it.</b> Both
            publishers derived their own genre, mood and tag nodes from their own
            tracks. Identical content produces an identical address, so both arrived at
            the same node and the two graphs simply meet there — no linkset entry, no
            shared schema registry, no coordination.
          </div>
        </div>
      )}

      {/* Recommendations first once the session has any signal — the page should
          answer what you've done, not repeat what it showed on load. */}
      <KernelRail railVersion={railVersion} stats={stats} />

      {/* One row per root. Same grid, same cards — the only difference is which key
          signed them, which is exactly the point being made. */}
      {artistTracks.length > 0 && (
        <>
          <div className="section-head">
            <h2>From {b?.label ?? "the artist"}'s own repo</h2>
            <span className="count">published by {shortAddr(b?.owner)}</span>
          </div>
          <div className="grid">
            {artistTracks.map((t) => <Card key={t.id} rec={t} queue={artistTracks} />)}
          </div>
        </>
      )}

      {platformTracks.length > 0 && (
        <>
          <div className="section-head">
            <h2>Most played on the platform</h2>
            <span className="count">published by {shortAddr(a?.owner)}</span>
          </div>
          <div className="grid">
            {platformTracks.map((t) => <Card key={t.id} rec={t} queue={platformTracks} />)}
          </div>
        </>
      )}
    </>
  );
}
