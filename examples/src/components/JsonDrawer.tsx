import { useState } from 'react';
import styles from './JsonDrawer.module.css';

interface Props {
  payload: object;
  open: boolean;
  onClose: () => void;
}

export default function JsonDrawer({ payload, open, onClose }: Props) {
  const [copied, setCopied] = useState(false);
  if (!open) return null;

  const json = JSON.stringify(payload, null, 2);

  async function copy() {
    try {
      await navigator.clipboard.writeText(json);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  }

  return (
    <div className={styles.drawer}>
      <div className={styles.header}>
        <span className={styles.title}>Raw payload</span>
        <div className={styles.actions}>
          <button type="button" className={styles.btn} onClick={copy}>
            {copied ? 'Copied' : 'Copy'}
          </button>
          <button type="button" className={styles.btn} onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
      </div>
      <pre className={styles.code}>
        <code dangerouslySetInnerHTML={{ __html: highlight(json) }} />
      </pre>
    </div>
  );
}

// Minimal JSON syntax highlighter (key=muted, string=green, number=blue, null=red).
function highlight(json: string): string {
  const esc = json
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  return esc.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (match) => {
      let cls = 'num';
      if (/^"/.test(match)) {
        cls = /:$/.test(match) ? 'key' : 'str';
      } else if (/true|false/.test(match)) {
        cls = 'bool';
      } else if (/null/.test(match)) {
        cls = 'null';
      }
      return `<span class="${cls}">${match}</span>`;
    },
  );
}
