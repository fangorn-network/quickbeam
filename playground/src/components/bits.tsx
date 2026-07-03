// A one-line copyable CID/hash/address, monospaced and middle-truncated.
export function Cid({ value, label }: { value: string | null | undefined; label?: string }) {
  if (!value) return <span className="cid muted">—</span>;
  const short = value.length > 22 ? `${value.slice(0, 10)}…${value.slice(-8)}` : value;
  return (
    <button className="cid" title={`Copy ${value}`} onClick={() => navigator.clipboard?.writeText(value)}>
      {label ? <span className="cid-label">{label}</span> : null}
      <code>{short}</code>
    </button>
  );
}

// Read-only pretty JSON block for the "published data" viewer.
export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}
