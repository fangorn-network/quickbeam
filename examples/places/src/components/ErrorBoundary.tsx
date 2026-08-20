// A last line of defense: if a page throws during render (a bad payload, a map/
// WebGL hiccup, an unexpected shape from the data), show a calm fallback instead of
// a blank white screen. The app shell (TopBar / nav) lives outside this boundary,
// so the user can always navigate away. App keys it on the route, so moving to a
// different page clears the error automatically.
import { Component, type ReactNode } from 'react';
import styles from './ErrorBoundary.module.css';

interface Props {
  children: ReactNode;
}
interface State {
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error) {
    // Surface it for diagnostics without taking the app down.
    console.error('[app] page crashed:', error);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className={styles.wrap} role="alert">
        <div className={styles.card}>
          <div className={styles.mark} aria-hidden="true">◇</div>
          <h2 className={styles.title}>Something went sideways</h2>
          <p className={styles.body}>
            This page hit a snag. Your likes and search are safe — try again, or head back home.
          </p>
          <div className={styles.actions}>
            <button type="button" className={styles.primary} onClick={() => this.setState({ error: null })}>
              Try again
            </button>
            <a className={styles.secondary} href="/">Go home</a>
          </div>
        </div>
      </div>
    );
  }
}
