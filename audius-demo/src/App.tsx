import { useEffect, useState } from 'react';
import KernelStrip from './components/KernelStrip';
import NowPlaying from './components/NowPlaying';
import Onboarding from './components/Onboarding';
import SearchBar from './components/SearchBar';
import { kernelSnapshot, onProgress, ready, type Progress } from './lib/client';
import { useKernel } from './lib/kernel';
import { goAbout, goHome, goPrivacy } from './lib/router';
import { useRoute } from './lib/router';
import type { Stats } from './lib/types';
import About from './views/About';
import Entity from './views/Entity';
import Privacy from './views/Privacy';
import Home from './views/Home';
import Search from './views/Search';

export default function App() {
  const route = useRoute();
  const [stats, setStats] = useState<Stats | null>(null);
  const [prog, setProg] = useState<Progress>({ phase: 'start', pct: 0 });
  const [err, setErr] = useState<string | null>(null);
  const { snap, refresh, onboarded } = useKernel();
  /** Pages that stand outside the graph UI, and outside the onboarding gate. */
  const isStatic = route.view === 'about' || route.view === 'privacy';

  useEffect(() => {
    onProgress(setProg);
    ready().then(setStats).catch((e) => setErr(e.message));
  }, []);

  return (
    <div className="shell">
      <header className="topbar">
        <button className="brand" onClick={goHome} aria-label="Home">
          <span className="brand-mark">FANGORN</span>
          <span className="brand-sub">Audius on two roots</span>
        </button>
        {stats && onboarded && !isStatic && route.view !== 'home' && (
          <SearchBar initial={route.q} />
        )}
        {/* In the header rather than a footer: the fixed now-playing bar covers
            the bottom of the viewport, and a privacy link nobody can find is not
            meaningfully offered. Not gated on `stats` either — both must stay
            reachable when the snapshot fails to load, which is exactly when
            someone wants to know what this page is and what it stores. */}
        <nav className="topnav">
          <button
            className={`topnav-link ${route.view === 'about' ? 'is-on' : ''}`}
            aria-current={route.view === 'about' ? 'page' : undefined}
            onClick={goAbout}
          >
            About
          </button>
          <button
            className={`topnav-link ${route.view === 'privacy' ? 'is-on' : ''}`}
            aria-current={route.view === 'privacy' ? 'page' : undefined}
            onClick={goPrivacy}
          >
            Privacy
          </button>
        </nav>
      </header>

      <main className="main">
        {err && (
          <div className="empty">
            <h3>Couldn't load the snapshot</h3>
            <p>{err}</p>
            <p style={{ marginTop: 12 }}>
              Start the snapshot server with
              {' '}<code>quickbeam cdn serve --cdn-dir ./audius-build/cdn --cors --port 8090</code>.
            </p>
            <p style={{ marginTop: 8, color: 'var(--text-faint)', fontSize: 13 }}>
              Viewing over a tunnel? Tunnel the app port only — the snapshot is proxied
              at <code>/cdn</code> on this same origin. Pointing the browser at
              {' '}<code>localhost:8090</code> makes it look for the server on
              <em> your</em> device.
            </p>
          </div>
        )}

        {!stats && !err && (
          <div className="loading">
            <div className="spinner" />
            <div className="bar"><i style={{ width: `${prog.pct}%` }} /></div>
            <span>{prog.detail ?? 'Downloading the graph to your browser…'}</span>
            <span style={{ fontSize: 13, color: 'var(--text-faint)', maxWidth: '46ch', textAlign: 'center' }}>
              The whole snapshot is coming to you, once. After this, every search runs
              locally and nothing you type is sent anywhere.
            </span>
          </div>
        )}

        {/* About and Privacy sit OUTSIDE the onboarding gate on purpose: someone
            deciding whether to hand over their taste should be able to read what
            this is and what it stores BEFORE doing so, not after. */}
        {stats && route.view === 'about' && <About stats={stats} />}
        {route.view === 'privacy' && <Privacy />}

        {/* The snapshot has to be loaded first — the picker's options are read out
            of the graph, and seeding needs the vectors it ranks over. */}
        {stats && !isStatic && !onboarded && <Onboarding />}

        {stats && !isStatic && onboarded && (
          <>
            {snap && (
              <KernelStrip snap={snap} onReset={() => { void kernelSnapshot().then(refresh); }} />
            )}
            {route.view === 'home' && <Home stats={stats} />}
            {route.view === 'search' && <Search q={route.q} />}
            {route.view === 'entity' && <Entity id={route.id} />}
          </>
        )}
      </main>

      <NowPlaying />
    </div>
  );
}
