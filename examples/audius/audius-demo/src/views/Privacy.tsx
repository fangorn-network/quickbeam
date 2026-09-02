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
        this browser and nowhere else: another device will not see them. You can
        export them to a file you keep and import that file back; the import is read
        and parsed in this tab, not sent anywhere.
      </p>

      <h2>Contact</h2>
      <p>
        Questions can go to{' '}
        <a href="mailto:fangorn@fangorn.network">fangorn@fangorn.network</a>. This
        policy may change if the demo does; the date below is the last revision.
      </p>
      <p className="about-lede" style={{ fontSize: 14 }}>Last updated 2 September 2026.</p>

      <button className="back" onClick={goHome}>← Back to the graph</button>
    </section>
  );
}
