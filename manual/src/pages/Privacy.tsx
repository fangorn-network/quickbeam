// Privacy policy. Every claim here is checked against the code: no cookie/localStorage/
// sessionStorage use anywhere in src/, search runs fully client-side (embed.ts + data.ts),
// and the only third-party hosts are Google Fonts (index.html) and the Hugging Face model
// CDN (embed.ts). If any of those change, change this page in the same commit.
//
// One claim here depends on a VENDOR behavior, not on our architecture: search queries ride in
// the URL (util.ts searchHref -> /search?q=…), and they stay private from analytics only because
// Cloudflare Web Analytics does not log query strings. Their FAQ says "currently … but we may add
// support for this in the future". If that changes, queries start reaching Cloudflare — fix it by
// moving the query out of the URL (POST-style state or sessionStorage), not by editing this page.
export default function Privacy() {
  return (
    <div className="reading">
      <div className="hero">
        <h1>Privacy</h1>
        <p>
          This site is a static copy of the Surge XT manual. There are no accounts, no logins
          and no forms — so there is very little to collect, and we collect very little.
        </p>
      </div>

      <div className="section-block">
        <h2>What we collect</h2>
        <p>
          <b>Aggregate page views, via Cloudflare Web Analytics.</b> It records that a page was
          loaded, along with the page path, the referring link, and coarse details derived from
          the request such as country, browser and device type. It is <b>cookieless</b>: it sets
          nothing on your device, does not fingerprint your browser, and cannot follow you to
          other sites.
        </p>
        <p>
          It records the path only, never the query string — and on this site the query string is
          where the content lives. Sections, effects, filters and patches are all addressed as{' '}
          <code>/entity?id=…</code>, and searches as <code>/search?q=…</code>, so what reaches the
          analytics is only the bucket: <code>/entity</code>, <code>/search</code>,{' '}
          <code>/</code>. We can see how many pages were opened. We cannot see which section,
          filter or patch you read.
        </p>
        <p>
          <b>Standard hosting logs.</b> The site is served by Cloudflare Pages, which processes
          your IP address in the ordinary course of delivering the page. We do not sell or share
          any of this, and none of it is used for advertising or profiling.
        </p>
      </div>

      <div className="section-block">
        <h2>What never leaves your browser</h2>
        <p>
          <b>Your searches.</b> Search is not a server request. The whole manual index is
          downloaded once as a static file, and your query is turned into a vector and matched
          against it entirely inside your browser. There is no search server to send a query to,
          and we never receive one.
        </p>
        <p className="muted">
          So that this is not overstated: your query does appear in the page address
          (<code>/search?q=…</code>), so it lands in your own browser history and in any link you
          copy or share. Cloudflare's analytics does not record query strings, and your browser
          sends only this site's domain — not the path or query — as the referrer on requests to
          the third parties below.
        </p>
        <p>
          <b>Which figures you view.</b> The manual's images are delivered as one identical
          bundle, downloaded once by everyone. Viewing a diagram makes no further request, so
          image traffic reveals nothing about what you were reading.
        </p>
      </div>

      <div className="section-block">
        <h2>Other services your browser contacts</h2>
        <p>
          Loading this site causes your browser to make requests to a few third parties, which
          will see your IP address and browser details as a normal part of serving those files:
        </p>
        <ul>
          <li>
            <b>Google Fonts</b> (<code>fonts.googleapis.com</code>,{' '}
            <code>fonts.gstatic.com</code>) — the typefaces used across the site.
          </li>
          <li>
            <b>Hugging Face</b> (<code>huggingface.co</code>) — the first time you search, the
            search model is downloaded from their CDN. It runs locally after that.
          </li>
          <li>
            <b>Cloudflare</b> — hosting, and the analytics described above.
          </li>
        </ul>
        <p className="muted">
          Each of these has its own privacy policy, which governs what they do with that request.
        </p>
      </div>

      <div className="section-block">
        <h2>Storage on your device</h2>
        <p>
          This site sets no cookies and stores nothing to identify or remember you. The only
          thing kept locally is the search model, which your browser caches after the first
          search so later searches are instant. Clearing this site's data removes it. The
          light/dark toggle is not saved — it resets on reload.
        </p>
      </div>

      <div className="section-block">
        <h2>Contact</h2>
        <p>
          Questions about this page can go to{' '}
          <a href="mailto:fangorn@fangorn.network">fangorn@fangorn.network</a>. This policy may
          change if the site does; the date below is the last revision.
        </p>
        <p className="muted">Last updated 28 July 2026.</p>
      </div>
    </div>
  );
}
