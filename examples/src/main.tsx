import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { DomainProvider } from './lib/domainContext';
import { TripProvider } from './lib/trip';
import { AuthProvider } from './lib/auth';
import './index.css';
import Footer from './components/Footer';

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
