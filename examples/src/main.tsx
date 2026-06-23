import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { DomainProvider } from './lib/domainContext';
import './index.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <DomainProvider>
        <App />
      </DomainProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
