import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { searchHref } from '../lib/util';

export default function SearchBox() {
  const [q, setQ] = useState('');
  const navigate = useNavigate();
  return (
    <form
      className="searchbox"
      onSubmit={(e) => {
        e.preventDefault();
        if (q.trim()) navigate(searchHref(q.trim()));
      }}
    >
      <span className="mag">⌕</span>
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search the manual + patches by meaning…"
        aria-label="Search"
      />
    </form>
  );
}
