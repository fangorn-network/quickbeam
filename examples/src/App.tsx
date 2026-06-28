import { useCallback, useEffect, useState } from 'react';
import { Routes, Route, useNavigate, useParams, useLocation } from 'react-router-dom';
import TopBar from './components/TopBar';
import LeftRail from './components/LeftRail';
import CommandPalette from './components/CommandPalette';
import { useBackStack, pushPage } from './hooks/useBackStack';
import { useTypeCounts } from './hooks/useTypeCounts';
import { useDomain } from './lib/domainContext';
import type { EntityType, PageRef } from './lib/types';
import { browseHref } from './lib/nav';
import { warmEmbedder } from './lib/embed';
import { IS_MOCK } from './lib/config';
import { useTrip } from './lib/trip';
import Landing from './pages/Landing';
import EntityPage from './pages/EntityPage';
import Results from './pages/Results';
import Trip from './pages/Trip';
import Atlas from './pages/Atlas';
import Ask from './pages/Ask';
import styles from './App.module.css';

// The raw type token from the path/query, if any (validated against the domain below).
function rawTypeFromPath(pathname: string, search: string): string | null {
  const browseMatch = pathname.match(/^\/browse\/([^/]+)/);
  if (browseMatch) return decodeURIComponent(browseMatch[1]);
  return new URLSearchParams(search).get('type');
}

export default function App() {
  const navigate = useNavigate();
  const location = useLocation();
  const domain = useDomain();
  const { recent, pop, canGoBack } = useBackStack();
  const { counts, connectionError } = useTypeCounts();
  const { items: tripItems } = useTrip();
  const [cmdkOpen, setCmdkOpen] = useState(false);
  const [railOpen, setRailOpen] = useState(false); // mobile nav drawer

  // Close the mobile nav drawer on any navigation.
  useEffect(() => {
    setRailOpen(false);
  }, [location.pathname, location.search]);

  const rawType = rawTypeFromPath(location.pathname, location.search);
  const activeType: EntityType | null = rawType && domain.hasType(rawType) ? rawType : null;

  // Start downloading the query-embedding model in the background so the first
  // semantic search isn't blocked on a cold model load. Mock mode never embeds.
  useEffect(() => {
    if (!IS_MOCK) warmEmbedder();
  }, []);

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
    // Default (no attribute) is the light "paper" theme, so flip to dark first.
    const root = document.documentElement;
    const isDark = root.getAttribute('data-theme') === 'dark';
    root.setAttribute('data-theme', isDark ? 'light' : 'dark');
  }

  return (
    <div className={styles.app}>
      <TopBar
        onCmdK={() => setCmdkOpen(true)}
        onBack={onBack}
        canGoBack={canGoBack}
        connectionError={connectionError}
        onToggleTheme={toggleTheme}
        onMenu={() => setRailOpen((o) => !o)}
        onHome={() => navigate('/')}
        onAtlas={() => navigate('/atlas')}
        onAsk={() => navigate('/ask')}
        tripCount={tripItems.length}
        onTrip={() => navigate('/trip')}
      />
      <div className={styles.body}>
        <LeftRail
          activeType={activeType}
          counts={counts}
          recent={recent}
          onTypeSelect={onTypeSelect}
          open={railOpen}
          onClose={() => setRailOpen(false)}
        />
        <main className={styles.main}>
          <Routes>
            <Route path="/" element={<Landing counts={counts} onVisit={onVisit} />} />
            <Route path="/browse/:entityType" element={<BrowseRoute onVisit={onVisit} />} />
            <Route path="/search" element={<Results onVisit={onVisit} />} />
            <Route path="/atlas" element={<Atlas />} />
            <Route path="/ask" element={<Ask onVisit={onVisit} />} />
            <Route path="/trip" element={<Trip />} />
            <Route path="/entity/:pointId" element={<EntityRoute onVisit={onVisit} />} />
          </Routes>
        </main>
      </div>
      {/* <div> */}
        {/* <Footer /> */}
      {/* </div> */}
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
