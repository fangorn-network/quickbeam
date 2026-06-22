import { useCallback, useEffect, useState } from 'react';
import { Routes, Route, useNavigate, useParams, useLocation } from 'react-router-dom';
import TopBar from './components/TopBar';
import LeftRail from './components/LeftRail';
import CommandPalette from './components/CommandPalette';
import { useBackStack, pushPage } from './hooks/useBackStack';
import { useTypeCounts } from './hooks/useTypeCounts';
import { isEntityType } from './lib/types';
import type { EntityType, PageRef } from './lib/types';
import { browseHref } from './lib/nav';
import Landing from './pages/Landing';
import EntityPage from './pages/EntityPage';
import Results from './pages/Results';
import styles from './App.module.css';

function activeTypeFromPath(pathname: string, search: string): EntityType | null {
  const browseMatch = pathname.match(/^\/browse\/([^/]+)/);
  if (browseMatch) {
    const t = decodeURIComponent(browseMatch[1]);
    if (isEntityType(t)) return t;
  }
  const params = new URLSearchParams(search);
  const t = params.get('type');
  if (t && isEntityType(t)) return t;
  return null;
}

export default function App() {
  const navigate = useNavigate();
  const location = useLocation();
  const { recent, pop, canGoBack } = useBackStack();
  const { counts, connectionError } = useTypeCounts();
  const [cmdkOpen, setCmdkOpen] = useState(false);

  const activeType = activeTypeFromPath(location.pathname, location.search);

  // Global Cmd-K / Ctrl-K.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setCmdkOpen((o) => !o);
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

  const onBack = useCallback(() => {
    const prev = pop();
    if (prev) navigate(prev.href);
    else navigate('/');
  }, [pop, navigate]);

  const onTypeSelect = useCallback(
    (t: EntityType | null) => {
      navigate(t ? browseHref(t) : '/');
    },
    [navigate],
  );

  const onVisit = useCallback((page: PageRef) => pushPage(page), []);

  function toggleTheme() {
    const root = document.documentElement;
    const cur = root.getAttribute('data-theme');
    root.setAttribute('data-theme', cur === 'light' ? 'dark' : 'light');
  }

  return (
    <div className={styles.app}>
      <TopBar
        onCmdK={() => setCmdkOpen(true)}
        onBack={onBack}
        canGoBack={canGoBack}
        connectionError={connectionError}
        onToggleTheme={toggleTheme}
      />
      <div className={styles.body}>
        <LeftRail
          activeType={activeType}
          counts={counts}
          recent={recent}
          onTypeSelect={onTypeSelect}
        />
        <main className={styles.main}>
          <Routes>
            <Route path="/" element={<Landing counts={counts} onVisit={onVisit} />} />
            <Route path="/browse/:entityType" element={<BrowseRoute onVisit={onVisit} />} />
            <Route path="/search" element={<Results onVisit={onVisit} />} />
            <Route path="/entity/:pointId" element={<EntityRoute onVisit={onVisit} />} />
          </Routes>
        </main>
      </div>
      <CommandPalette
        open={cmdkOpen}
        onClose={() => setCmdkOpen(false)}
        recent={recent}
        onVisit={onVisit}
      />
    </div>
  );
}

// Browse-by-type reuses the Results page with a fixed type and no query.
function BrowseRoute({ onVisit }: { onVisit: (p: PageRef) => void }) {
  const { entityType } = useParams();
  return <Results onVisit={onVisit} browseType={entityType} />;
}

function EntityRoute({ onVisit }: { onVisit: (p: PageRef) => void }) {
  const { pointId } = useParams();
  return <EntityPage pointId={pointId ?? ''} onVisit={onVisit} />;
}
