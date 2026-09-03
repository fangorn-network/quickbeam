// Privacy policy. Every claim here was checked against this app's code, not copied
// from the Surge XT manual's — the two differ in ways that matter:
//
//   • This app DOES use localStorage, under TWO keys: the taste kernel
//     (lib/kernel.tsx, "audius-demo.kernel") and your playlists (lib/playlists.tsx,
//     "audius-demo.playlists"). The manual stores nothing. Do not import that
//     sentence from it. scripts/check-playlists.ts fails if this page stops naming
//     the playlist key.
//   • Artwork is fetched PER RECORD from Audius' content node (lib/format.ts
//     artUrl), so browsing does disclose what you are looking at. The manual
//     bundles its images and can honestly claim the opposite.
//   • Search rides in the HASH (lib/router.ts, #/search?q=…), which browsers
//     never transmit — stronger than the manual, whose query is a real query
//     string and relies on Cloudflare not logging it.
//   • An AGENT in this browser can drive the page (lib/webmcp.ts, registered on
//     document.modelContext). It is behind a Chrome flag, so it is absent for
//     almost every visitor — but where present it can read the taste kernel
//     (`read-taste`) and write playlists, which is a disclosure this page has to
//     name. It sends nothing anywhere: the tools call the same in-tab worker the
//     UI does. check-webmcp.ts fails if this page stops naming `read-taste`.
//   • Playlists can be SHARED as a link (lib/playlists.tsx shareUrl →
//     lib/router.ts shareHref, #/playlists?s=…). The payload is in the fragment,
//     so it is not transmitted either — but it does leave this browser in the
//     hands of whoever you send it to, which is why the paragraph below says the
//     link is the data. check-playlists.ts fails if this page stops saying so.
//
// Hosts, verified: creatornode.audius.co + discoveryprovider.audius.co
// (lib/config.ts), huggingface.co (transformers.js default remote host — nothing
// in lib/graph.ts overrides env.remoteHost), and Cloudflare Pages for hosting.
// Figtree is bundled via @fontsource-variable/figtree and self-hosted, so there
// is NO Google Fonts request. If any of that changes, change this page with it.
//
// ⚠ ONE UNVERIFIED CLAIM: the Cloudflare Web Analytics paragraph. This app ships
// no analytics script of its own (index.html is clean; no beacon anywhere in
// src/). It is described below because the sibling deploy uses it and a project
// can have it injected from the Cloudflare dashboard without touching this repo.
// Over-disclosing is the safe direction — but if analytics is NOT enabled on the
// audius-demo Pages project, delete that paragraph.
import { goHome } from '../lib/router';

export default function Privacy() {
  return (
    <section className="about">
      <div className="hero-kicker">Privacy</div>
      <h1>What this page <em>can</em> know.</h1>
      <p className="about-lede">
        There are no accounts, no logins and no wallet connection here. The whole
        graph is downloaded to your browser once and searched there, so most of what
        you do never reaches a server at all — including the only text you ever type,
        a search or a playlist name. The exceptions are worth stating plainly,
        because there are some.
      </p>

      <h2>What never leaves your browser</h2>
      <p>
        <b>Your searches.</b> Search is not a server request. The entire snapshot
        is fetched once as static files, and your query is turned into a vector
        and matched against it inside a worker in this tab. There is no search
        server to send a query to.
      </p>
      <p>
        <b>Not even in the address bar.</b> Searches are addressed as{' '}
        <code>#/search?q=…</code> — a URL <i>fragment</i>. Browsers never transmit
        the part after <code>#</code> to a server, so your query does not appear
        in hosting logs or analytics even in principle. It is still in your own
        browser history and in any link you copy.
      </p>
      <p>
        <b>Your taste profile.</b> The recommender that drives "Where you're
        heading" runs entirely here. What you play, skip, like and dislike is
        folded into a model held in this tab and saved to this browser's local
        storage so it survives a reload. It is never uploaded, and there is no
        account for it to be attached to.
      </p>

      <h2>If you use an AI agent in this browser</h2>
      <p>
        This page offers its own tools to a browser-resident agent, so one can
        search the graph, work the player and build playlists for you without a
        server in the middle. Almost nobody has this: it needs a browser flag that
        is off by default, and where it is off the feature does not exist and
        nothing below applies.
      </p>
      <p>
        Where it <i>is</i> on, an agent you are using can read the same things you
        can see — including your taste profile, through a tool called{' '}
        <code>read-taste</code>. That is worth stating plainly, because it is the
        one way the profile described above leaves the page at all. It does not
        leave it to <em>us</em>: every tool runs against the snapshot already in
        this tab, and none of them sends anything to a server. What the agent then
        does with what it read is between you and whoever makes your agent.
      </p>
      <p>
        Two tools can change your playlists. Making a <b>new</b> playlist happens
        without interrupting you, because you asked for it and nothing you already
        had is touched. Adding tracks to a playlist you <b>already made</b> puts a
        card on the screen and waits: if you dismiss it, or simply do not answer,
        nothing is added. No tool can rename or delete anything, and none of them
        can spend money — there is nothing here to spend it on.
      </p>

      <h2>What Audius can see</h2>
      <p>
        This demo is built on real Audius data and plays real audio, which means
        Audius' own servers are involved whenever media loads:
      </p>
      <ul className="about-caveats">
        <li>
          <b>Tracks you play.</b> Audio streams from Audius' discovery node, so
          playing a track tells them which track, from which app, along with your
          IP address. There is no way to stream their catalogue without this.
        </li>
        <li>
          <b>Artwork you load — which means pages you look at.</b> Cover images
          are fetched per record from Audius' content node as you browse. That
          traffic reveals which artists and tracks you viewed, not only the ones
          you played. This is the one place where browsing is not private, and
          it applies whether or not you press play.
        </li>
      </ul>

      <h2>Other services your browser contacts</h2>
      <ul className="about-caveats">
        <li>
          <b>Hugging Face</b> (<code>huggingface.co</code>) — the first time you
          search, the embedding model is downloaded from their CDN. It runs
          locally from then on.
        </li>
        <li>
          <b>Cloudflare</b> — hosting. It processes your IP address in the
          ordinary course of serving the page.
        </li>
      </ul>
      <p>
        Typefaces are served from this site itself, not from a font CDN. Each
        third party above has its own privacy policy governing that request.
      </p>

      <h2>Analytics</h2>
      <p>
        Aggregate page views may be recorded via <b>Cloudflare Web Analytics</b>,
        which is <b>cookieless</b>: it sets nothing on your device, does not
        fingerprint your browser, and cannot follow you to other sites. Because
        every view in this app is addressed with a <code>#</code> fragment, what
        it can record is the bare path — not which artist, track or search you
        opened. Nothing here is sold, shared, or used for advertising or
        profiling.
      </p>

      <h2>Storage on your device</h2>
      <p>
        No cookies. Three things are kept locally: your taste profile, under the
        key <code>audius-demo.kernel</code>; your playlists, under{' '}
        <code>audius-demo.playlists</code>; and the search model, which your browser
        caches after the first search. Clearing this site's data removes all three.
        You can reset the taste profile at any time from the readout strip at the top
        of the page, without clearing anything else.
      </p>

      <p>
        <b>What a playlist is.</b> A name and a list of record ids — never a copy of
        the audio or the artwork, which stay where they already are. Playlists are
        never uploaded and there is no account to attach them to, so they live in
        this browser unless you deliberately move them. You can export them to a file
        you keep and import that file back; the import is read and parsed in this
        tab, not sent anywhere.
      </p>
      <p>
        <b>Sharing a playlist.</b> "Copy link" does not upload anything, because there
        is nowhere to upload it to. The playlist's name and record ids are encoded{' '}
        <i>into the link itself</i>, after the <code>#</code> — so, exactly like a
        search, it is never transmitted to this site's host and cannot appear in its
        logs or analytics. The trade is that <b>the link is the data</b>: anyone
        holding it can read the playlist, and it travels wherever you paste it.
        Opening someone else's link only shows it to you — nothing is written to this
        browser until you press Save.
      </p>

      <h2>Contact</h2>
      <p>
        Questions can go to{' '}
        <a href="mailto:fangorn@fangorn.network">fangorn@fangorn.network</a>. This
        policy may change if the demo does; the date below is the last revision.
      </p>
      <p className="about-lede" style={{ fontSize: 14 }}>Last updated 3 September 2026.</p>

      <button className="back" onClick={goHome}>← Back to the graph</button>
    </section>
  );
}
