// Privacy policy for the LARGE build. Diverges from audius-demo's on purpose: that
// app ships its whole corpus and can honestly say search never touches the network.
// This one cannot, and saying so anyway would be the most misleading sentence on the
// site. Do not copy claims between the two.
//
// WHAT WOULD FALSIFY THE SEARCH CLAIM: any code path outside src/lib/store.ts that
// calls fetch/XHR/WebSocket in the retrieval flow, or any request whose URL or body
// carries a vector, a query string, or a per-client random value. `npm run
// check:private` executes exactly that as a test — it records every request a real
// query makes and asserts the URLs are bare /bucket/{n} with empty bodies.
//
// THE NUMBERS QUOTED BELOW ARE MEASURED, not aspirational — but they come from two
// different measurements and only one is of THIS build:
//   * 464 buckets of 8 cells — this build, read from index/layout.json.
//   * genre disclosure — this build: a cell predicts genre at 0.802, a bucket at
//     0.337, against a 0.178 baseline, over all 1,391,364 records carrying a genre.
//   * the "three- to sevenfold narrowing" over a themed session — measured on a
//     500k subset at k=977 with the SAME bucket size of 8, over 40 trials
//     (audius-status.txt). Not re-run against this 1.9M codebook. The mechanism and
//     the bucket size are identical so it should carry, but if that range is ever
//     quoted as a hard guarantee it needs re-measuring here first.
// If the codebook is refit with different parameters, ALL of these change.
//
// Every other claim here was checked against this app's code, not copied
// from the Surge XT manual's — the two differ in ways that matter:
//
//   • This app DOES use localStorage (lib/kernel.tsx, key "audius-demo.kernel").
//     The manual stores nothing. Do not import that sentence from it.
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
        There are no accounts, no logins, no wallet connection and no forms here.
        A starting slice of the catalogue is downloaded to your browser and searched
        there; the rest is far too large to send, so searching it does involve a
        server — but never one that is told what you typed. How that works, and what
        it does not cover, is worth stating plainly.
      </p>

      <h2>What never leaves your browser</h2>
      <p>
        <b>Your query itself.</b> This catalogue is far too large to send to you,
        so unlike the smaller demo it is <i>not</i> all in this tab — and yet your
        query still never leaves it. Your words are turned into a vector here, and
        that vector is compared here against a public codebook you download once.
        What actually goes to the server is a single number: which of{' '}
        <b>464 public buckets</b> to send back. Each bucket holds eight unrelated
        regions of the catalogue, the same eight for every visitor, so the request
        does not say which one you wanted. The results are ranked against your real
        query back here, in this tab.
      </p>
      <p>
        <b>What that does and does not hide.</b> A single search tells the server
        little more than the overall shape of the catalogue. Because buckets are
        cached, searching the same area again sends nothing at all. But searching
        one theme repeatedly, in one sitting, does narrow what could be inferred —
        we measured roughly a three- to sevenfold narrowing over four related
        searches, which is a long way from identifying your query but is not
        nothing. We would rather say so than round it down to "anonymous".
      </p>
      <p>
        <b>Browsing is not private, and never was.</b> Opening a record asks the
        server for that record and its connections, and its artwork is fetched from
        Audius' own servers. So the pages you <i>open</i> are visible in a way the
        things you <i>search for</i> are not. That distinction is the whole design,
        and it would be misleading to describe the second without the first.
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
        No cookies. Two things are kept locally: your taste profile, under the
        key <code>audius-demo.kernel</code>, and the search model, which your
        browser caches after the first search. Clearing this site's data removes
        both. You can reset the taste profile at any time from the readout strip
        at the top of the page, without clearing anything else.
      </p>

      <h2>Contact</h2>
      <p>
        Questions can go to{' '}
        <a href="mailto:fangorn@fangorn.network">fangorn@fangorn.network</a>. This
        policy may change if the demo does; the date below is the last revision.
      </p>
      <p className="about-lede" style={{ fontSize: 14 }}>Last updated 4 August 2026.</p>

      <button className="back" onClick={goHome}>← Back to the graph</button>
    </section>
  );
}
