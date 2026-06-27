import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { DomainProvider } from './lib/domainContext';
import { TripProvider } from './lib/trip';
import { AuthProvider } from './lib/auth';
import './index.css';
import Footer from './components/Footer';
import { LANG, communityFull } from './lib/i18n';

// Reflect the active locale on the document: <html lang> for a11y / hyphenation,
// and the title so the tab and shares match the configured community + language.
document.documentElement.lang = LANG;
document.title = `SOND3R · ${communityFull}`;

// AuthProvider sits BELOW Router/Domain/Trip: it lazy-mounts Privy on first login,
// and that swap remounts only its subtree — keeping trip/router state intact (and
// the remount is hidden behind the login modal anyway). Footer needs no auth, so
// it stays outside.
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <DomainProvider>
        <TripProvider>
          <AuthProvider>
            <App />
          </AuthProvider>
          <Footer />
        </TripProvider>
      </DomainProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
