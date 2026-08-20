// Lazy auth seam. Privy (~830 KB) is the single heaviest dependency, yet login is
// OPTIONAL — it gates only actions (claim/tip), never discovery. So we keep
// @privy-io/react-auth out of the initial bundle entirely and load it on demand,
// the first time someone actually clicks "Sign in". Anonymous visitors never pay
// for it; first paint on a cold gateway isn't blocked parsing it.
//
// Components consume `useAuth()` — a tiny stable surface ({ready, authenticated,
// user, login, logout}) — instead of `usePrivy()` directly, so they render fine
// whether or not Privy has loaded. While unloaded, `login()` triggers the import
// and auto-opens the modal once Privy is ready.
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { PRIVY_APP_ID } from './config';
import { NETWORK } from './network';

type PrivyModule = typeof import('@privy-io/react-auth');

export interface AuthUser {
  email?: { address?: string } | null;
}

export interface AuthState {
  ready: boolean;
  authenticated: boolean;
  user: AuthUser | null;
  login: () => void;
  logout: () => void | Promise<void>;
}

const STUB: AuthState = {
  ready: false,
  authenticated: false,
  user: null,
  login: () => {},
  logout: () => {},
};

const AuthContext = createContext<AuthState>(STUB);
export function useAuth(): AuthState {
  return useContext(AuthContext);
}

// Bridges Privy's live state into our context — only rendered once the module is
// loaded, so calling its hook here is safe. If a login was requested before Privy
// finished loading, open the modal as soon as it's ready.
function PrivyBridge({
  mod,
  children,
  wantLogin,
  onConsumed,
}: {
  mod: PrivyModule;
  children: ReactNode;
  wantLogin: boolean;
  onConsumed: () => void;
}) {
  const { ready, authenticated, user, login, logout } = mod.usePrivy();

  useEffect(() => {
    if (ready && wantLogin) {
      login();
      onConsumed();
    }
  }, [ready, wantLogin, login, onConsumed]);

  const value = useMemo<AuthState>(
    () => ({ ready, authenticated, user: (user as AuthUser) ?? null, login, logout }),
    [ready, authenticated, user, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [mod, setMod] = useState<PrivyModule | null>(null);
  const [wantLogin, setWantLogin] = useState(false);

  const loadPrivy = useCallback(() => {
    void import('@privy-io/react-auth').then((m) => setMod((cur) => cur ?? m));
  }, []);

  // Stub login used until Privy is mounted: kick off the (lazy) load and remember
  // that the user wants the modal, which PrivyBridge opens once ready.
  const stubLogin = useCallback(() => {
    setWantLogin(true);
    loadPrivy();
  }, [loadPrivy]);

  const stubValue = useMemo<AuthState>(() => ({ ...STUB, login: stubLogin }), [stubLogin]);

  if (!mod) {
    return <AuthContext.Provider value={stubValue}>{children}</AuthContext.Provider>;
  }

  const Privy = mod.PrivyProvider;
  return (
    <Privy
      appId={PRIVY_APP_ID}
      config={{
        // Mint an embedded wallet for email users — that wallet is the Phase 4
        // claim/tip key. No wallet UIs: identity should feel like a normal login.
        embeddedWallets: {
          showWalletUIs: false,
          ethereum: { createOnLogin: 'users-without-wallets' },
        },
        // Active chain (Base) — used by Phase 4; harmless for login-only today.
        defaultChain: NETWORK.chain,
        supportedChains: [NETWORK.chain],
        appearance: { theme: 'light', accentColor: '#1f6f4f', showWalletLoginFirst: false },
        loginMethods: ['email'],
      }}
    >
      <PrivyBridge mod={mod} wantLogin={wantLogin} onConsumed={() => setWantLogin(false)}>
        {children}
      </PrivyBridge>
    </Privy>
  );
}
