import { useEffect, useState } from 'react';
import KernelStrip from './components/KernelStrip';
import NowPlaying from './components/NowPlaying';
import SearchBar from './components/SearchBar';
import { kernelSnapshot, onProgress, ready, type Progress } from './lib/client';
import { useKernel } from './lib/kernel';
import { goHome } from './lib/router';
import { useRoute } from './lib/router';
import type { Stats } from './lib/types';
import Entity from './views/Entity';
import Home from './views/Home';
import Search from './views/Search';

export default function App() {
  const route = useRoute();
  const [stats, setStats] = useState<Stats | null>(null);
  const [prog, setProg] = useState<Progress>({ phase: 'start', pct: 0 });
  const [err, setErr] = useState<string | null>(null);
  const { snap, refresh } = useKernel();

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
        {stats && route.view !== 'home' && <SearchBar initial={route.q} />}
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

        {stats && snap && (
          <KernelStrip snap={snap} onReset={() => { void kernelSnapshot().then(refresh); }} />
        )}
        {stats && route.view === 'home' && <Home stats={stats} />}
        {stats && route.view === 'search' && <Search q={route.q} />}
        {stats && route.view === 'entity' && <Entity id={route.id} />}
      </main>

      <NowPlaying />
    </div>
  );
}
