import { useState } from 'react';
import { goSearch } from '../lib/router';

export default function SearchBar({ initial = '', autoFocus = false }: { initial?: string; autoFocus?: boolean }) {
  const [q, setQ] = useState(initial);
  return (
    <form
      className="search"
      onSubmit={(e) => { e.preventDefault(); if (q.trim()) goSearch(q.trim()); }}
      role="search"
    >
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Describe what you want to hear"
        aria-label="Search the graph"
        autoFocus={autoFocus}
      />
      <button type="submit" disabled={!q.trim()}>Search</button>
    </form>
  );
}
