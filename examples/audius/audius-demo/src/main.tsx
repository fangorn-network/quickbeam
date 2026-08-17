import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '@fontsource-variable/figtree';
import './styles.css';
import App from './App';
import { KernelProvider } from './lib/kernel';
import { PlayerProvider } from './lib/player';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* Above App so the single <audio> element survives every view change —
        navigating between search and an entity page must not stop the music. */}
    {/* Kernel outside the player: the player reports plays and skips into it. */}
    <KernelProvider>
      <PlayerProvider>
        <App />
      </PlayerProvider>
    </KernelProvider>
  </StrictMode>,
);
