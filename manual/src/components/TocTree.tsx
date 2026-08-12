import { useState } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { useCorpus } from '../lib/corpus';
import { entityHref } from '../lib/util';
import type { TocNode } from '../lib/toc';

export default function TocTree() {
  const { toc } = useCorpus();
  return (
    <nav className="toc">
      <div className="toc-title">Contents</div>
      <ul>
        {toc.map((n) => <TreeItem key={n.id} node={n} depth={0} />)}
      </ul>
    </nav>
  );
}

function TreeItem({ node, depth }: { node: TocNode; depth: number }) {
  const navigate = useNavigate();
  const location = useLocation();
  const activeId = new URLSearchParams(location.search).get('id');
  const active = activeId === node.id;
  const hasKids = node.children.length > 0;
  const [open, setOpen] = useState(depth < 1);
  return (
    <li>
      <div className={`toc-row ${active ? 'active' : ''}`}>
        <span
          className="toc-caret"
          style={{ cursor: hasKids ? 'pointer' : 'default' }}
          onClick={(e) => { e.stopPropagation(); if (hasKids) setOpen((o) => !o); }}
        >
          {hasKids ? (open ? '▾' : '▸') : '·'}
        </span>
        <span className="toc-label" onClick={() => navigate(entityHref(node.id))}>
          {node.title}
        </span>
      </div>
      {hasKids && open && (
        <ul className="toc-children">
          {node.children.map((c) => <TreeItem key={c.id} node={c} depth={depth + 1} />)}
        </ul>
      )}
    </li>
  );
}
