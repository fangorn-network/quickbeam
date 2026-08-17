import { useEffect } from 'react';
import { Routes, Route, Link } from 'react-router-dom';
import { CorpusProvider } from './lib/corpus';
import { warmEmbedder } from './lib/embed';
import { imageBundle } from './lib/images';
import TocTree from './components/TocTree';
import SearchBox from './components/SearchBox';
import Home from './pages/Home';
import EntityView from './pages/EntityView';
import SearchResults from './pages/SearchResults';
import Privacy from './pages/Privacy';

function toggleTheme() {
  const r = document.documentElement;
  r.setAttribute('data-theme', r.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
}

export default function App() {
  // Warm the query-embedding model, and start the one-time figure-bundle download,
  // so both are ready by the time the user searches / opens a section.
  useEffect(() => { warmEmbedder(); imageBundle(); }, []);

  return (
    <div className="app">
      <header className="header">
        <Link to="/" className="brand">Surge&nbsp;XT <small>Manual</small></Link>
        <SearchBox />
        <span className="spacer" />
        <button className="iconbtn" onClick={toggleTheme} title="Toggle light / dark">◑</button>
      </header>
      <CorpusProvider>
        <div className="body">
          <aside className="sidebar"><TocTree /></aside>
          <main className="main">
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/entity" element={<EntityView />} />
              <Route path="/search" element={<SearchResults />} />
              <Route path="/privacy" element={<Privacy />} />
            </Routes>
          </main>
        </div>
      </CorpusProvider>
      <footer className="footer">
        <span>Surge XT manual — an unofficial searchable edition.</span>
        <Link to="/privacy">Privacy</Link>
      </footer>
    </div>
  );
}
