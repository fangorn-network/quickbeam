// What this site is, and what it is not.
//
// Every count on this page is read from the served snapshot rather than written
// into the prose: a hand-typed number is a claim that goes stale the next time
// anyone re-bakes, and this is the page a sceptical reader checks first.
import { isPlaceholderWallet } from '../lib/format';
import { goHome } from '../lib/router';
import type { Stats } from '../lib/types';

export default function About({ stats }: { stats: Stats }) {
  const unpublished = stats.publishers.some((p) => isPlaceholderWallet(p.owner));

  return (
    <section className="about">
      <div className="hero-kicker">About</div>
      <h1>A music catalogue that <em>runs in your tab</em>.</h1>
      <p className="about-lede">
        Fangorn Music searches {stats.records.toLocaleString()} records by what they sound like, not by what they are tagged. The whole index is downloaded once and everything, 
        from the search, the recommendations,  and the taste model, happens in this browser, with nothing sent to a server.
      </p>

      <h2>How the catalogue is published</h2>
      <p>
        The catalogue is not a database behind an API. It is a graph of
        content-addressed blocks: every vertex, edge and revision is named by a hash
        of its own content, and a publisher's entire state is one 32-byte pointer to
        the head of their own history. Reading it needs a gateway and nothing else —
        no indexer, no subgraph, no API key. Anyone can publish into it, which is why
        the tracks here carry a badge saying where they came from.
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
        process. The tab already holds the catalogue, so an agent's question is
        answered by the same local vector search yours is, and neither reaches a
        server.
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

      <h2>Where the music comes from</h2>
      <ul className="about-caveats">
        <li>
          <b>The music is Audius'.</b> Everything here was crawled from live Audius
          discovery nodes — real tracks, real artists, real remix and playlist
          relationships. Audio and artwork stream from Audius' own nodes as you
          browse, so roughly 1% of tracks are gated or withdrawn and will refuse to
          play. What each record's badge marks is exactly this: it came from Audius.
        </li>
        <li>
          <b>Their branding is borrowed, not claimed.</b> The Audius mark on each
          record is theirs, used to attribute the source and nothing else. The
          colours, radii and type scale come from their open-source Harmony design
          tokens; Avenir Next LT Pro is theirs and proprietary, so Figtree stands in.
          This site is Fangorn Music and is not an Audius property, nor affiliated
          with or endorsed by them.
        </li>
        {unpublished && (
          <li>
            <b>Not yet settled on-chain.</b> The publishers behind this catalogue are
            still placeholder wallets. The graph and its links are genuine and built
            by the real pipeline; the final step of anchoring each root to a contract
            has not been run for this dataset.
          </li>
        )}
      </ul>

      <button className="back" onClick={goHome}>← Back to the music</button>
    </section>
  );
}
