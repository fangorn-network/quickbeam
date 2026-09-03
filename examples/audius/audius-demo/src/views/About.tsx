// What this demo actually is, and what it is not.
//
// Every count on this page is read from the served snapshot rather than written
// into the prose, for the same reason RootLedger draws its strands from the real
// linkset: a hand-typed number is a claim that goes stale the next time anyone
// re-bakes, and this is the page a sceptical reader checks first.
import { isPlaceholderWallet, shortAddr } from '../lib/format';
import { goHome } from '../lib/router';
import type { Stats } from '../lib/types';

export default function About({ stats }: { stats: Stats }) {
  const [a, b] = stats.publishers;
  const artist = b?.label ?? 'the artist';
  const unpublished = stats.publishers.some((p) => isPlaceholderWallet(p.owner));

  return (
    <section className="about">
      <div className="hero-kicker">About this demo</div>
      <h1>Two publishers. <em>One graph.</em></h1>
      <p className="about-lede">
        A Fangorn demo built on real Audius data. Two independent publishers hold
        two separate graphs under two separate keys — and you browse, search and
        get recommendations straight across both, in this tab, with nothing sent
        to a server.
      </p>

      <h2>The split</h2>
      <p>
        Audius' premise is that artists publish their own music. So rather than
        embedding a catalogue and calling it decentralised, this demo builds the
        split that premise implies, and then shows the seam holding.
      </p>
      <dl className="about-defs">
        <dt>Publisher A — the platform <span className="about-addr">{shortAddr(a?.owner)}</span></dt>
        <dd>
          The trending and underground slice, {(a?.counts.Track ?? 0).toLocaleString()} tracks
          across {(a?.counts.Artist ?? 0).toLocaleString()} artists — with {artist}'s
          catalogue <b>removed</b>. A thin reference stub is left where it used to be.
        </dd>
        <dt>Publisher B — {artist} <span className="about-addr">{shortAddr(b?.owner)}</span></dt>
        <dd>
          Their own {(b?.counts.Track ?? 0).toLocaleString()} tracks, plus profile,
          releases and playlists, published from their own wallet.
        </dd>
        <dt>The linkset — nobody's</dt>
        <dd>
          <b>{stats.linksetTotal}</b> edges that join the two, which neither side can
          assert alone. Playlists on the platform that hold {artist}'s tracks; platform
          artists whose work is a remix of theirs.
        </dd>
      </dl>
      <p>
        The platform's graph has a hole shaped like the artist. The artist's graph
        fills it. {stats.records.toLocaleString()} records and{' '}
        {stats.edges.toLocaleString()} edges in total, and nothing had to be merged
        into one database to make that work.
      </p>

      <h2>Why the two halves fit</h2>
      <p>
        Every block is addressed by a hash of its own content. Both publishers derived
        genre, mood and tag nodes from their own tracks independently — identical
        content produces an identical address, so both arrived at the same node and
        the graphs simply meet there.{' '}
        <b>{stats.converged} vertices converged that way</b>: no linkset entry, no
        shared schema registry, no coordination. Content addressing doing the job a
        schema registry usually does.
      </p>
      <p>
        A publisher's entire state is one 32-byte pointer to the head of their own
        history. The graph itself — every vertex, edge and revision — lives off-chain
        as content-addressed blocks, so reading it needs a gateway and nothing else:
        no indexer, no subgraph, no API key.
      </p>

      <h2>What runs where</h2>
      <p>
        The whole snapshot is downloaded once, up front. After that it is all local:
        the embedding model, the {stats.records.toLocaleString()}-vector similarity
        scan and the session kernel all run in a Web Worker, off the main thread.
        Queries never leave the tab — <em>knowledge is public, intent is private</em>.
        That is also why the loading spinner is pure CSS: if it ever stops turning,
        something has escaped onto the main thread.
      </p>

      <h2>The third client: an agent in this tab</h2>
      <p>
        Two things already read this snapshot: this page, and{' '}
        <code>quickbeam mcp</code>, a pull-client that hands the same shards to an
        agent as tools — the snapshot goes to the process, so the queries never leave
        it. WebMCP is the third, and it is that argument taken to its end: there is no
        process. The tab already holds the graph, so an agent's question is answered by
        the same local vector search yours is, and neither reaches a server.
      </p>
      <p>
        Where the browser supports it — today that means a flag that is off by
        default — this page registers fourteen verbs an agent can call:{' '}
        <code>search-music</code>, <code>open-record</code>,{' '}
        <code>describe-graph</code>, <code>list-relations</code>,{' '}
        <code>traverse</code>, <code>browse</code>, <code>player-state</code>,{' '}
        <code>control-player</code>, <code>read-taste</code>, <code>recommend</code>,{' '}
        <code>list-playlists</code>, <code>create-playlist</code>,{' '}
        <code>add-to-playlist</code> and <code>share-playlist</code>.
      </p>
      <p>
        The last four are the ones a backend could not offer.{' '}
        <code>control-player</code> moves the same audio element the bar at the bottom
        drives, not a copy of it. <code>read-taste</code> reads a model that exists in
        this tab and nowhere else — there is no profile on a server to look up. And
        because search results carry each track's length and mood, an agent can answer
        a brief with a shape to it: <i>an hour long, high energy to start, tapering
        off by the end</i>. Ask it in those words and it can build that.
      </p>
      <p>
        Making a new playlist happens straight away. Changing one you already made
        asks first, and silence is not a yes. Nothing here deletes or renames.
      </p>

      <h2>The session kernel</h2>
      <p>
        "Where you're heading" is a geometric taste model, not a playlist. Your genre
        and artist picks seed it; from there it tracks a position and a velocity
        through the same embedding space the search runs over, and steers.
      </p>
      <p>
        It updates on <b>settled evidence only</b> — a track you let finish, or one you
        explicitly rate. Merely pressing play feeds the model but deliberately does not
        reshuffle the grid you just clicked into. Skipping a track you have played a lot
        reads as fatigue and mutes the artist for the session; skipping one you have
        never played reads as dislike, and enough of those suppress them for good. Your
        kernel is kept in this browser's local storage, so it survives a reload — and
        the strip at the top of the page will reset it whenever you want.
      </p>

      <h2>What's demo, and what's real</h2>
      <ul className="about-caveats">
        {unpublished && (
          <li>
            <b>Not yet settled on-chain.</b> Both publishers here are placeholder
            wallets. The graphs, the split and the linkset are genuine and built by
            the real pipeline; the final step of anchoring each root to a contract
            has not been run for this dataset.
          </li>
        )}
        <li>
          <b>The data is real.</b> Everything was crawled from live Audius discovery
          nodes — real tracks, real artists, real remix and playlist relationships.
          Audio and artwork stream from Audius' own nodes, so roughly 1% of tracks
          are gated or withdrawn and will refuse to play.
        </li>
        <li>
          <b>Branding is borrowed, not claimed.</b> Colours, radii and type scale come
          from Audius' own open-source Harmony design tokens. Avenir Next LT Pro is
          theirs and proprietary, so Figtree stands in. This page is badged Fangorn
          throughout and is not an Audius property.
        </li>
      </ul>

      <button className="back" onClick={goHome}>← Back to the graph</button>
    </section>
  );
}
