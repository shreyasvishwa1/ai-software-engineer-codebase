import { useState } from 'react'

import type { TreeDirectory, TreeNode } from '../types/api'
import { formatBytes } from '../utils/format'

function DirectoryNode({ node, depth }: { node: TreeDirectory; depth: number }) {
  const [open, setOpen] = useState(depth === 0)
  return (
    <li>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-1 rounded px-1 py-0.5 text-left text-slate-200 hover:bg-slate-800"
      >
        <span aria-hidden="true">{open ? '▾' : '▸'}</span>
        <span>{node.name}</span>
      </button>
      {open && <NodeList nodes={node.children} depth={depth + 1} />}
    </li>
  )
}

function NodeList({ nodes, depth }: { nodes: TreeNode[]; depth: number }) {
  return (
    <ul className={depth > 0 ? 'ml-4 border-l border-slate-800 pl-2' : ''} role="group">
      {nodes.map((node) =>
        node.type === 'directory' ? (
          <DirectoryNode key={`d:${node.path}`} node={node} depth={depth} />
        ) : (
          <li key={`f:${node.path}`} className="flex items-center gap-2 px-1 py-0.5 text-slate-300">
            <span>{node.name}</span>
            <span className="text-xs text-slate-500">{node.language ?? 'Unknown'}</span>
            <span className="ml-auto text-xs text-slate-500">{formatBytes(node.size_bytes)}</span>
          </li>
        ),
      )}
    </ul>
  )
}

/** Collapsible repository tree: top-level entries visible, nested directories collapsed. */
export function FileTree({ tree }: { tree: TreeDirectory }) {
  if (tree.children.length === 0) {
    return <p className="text-sm text-slate-400">No files have been ingested yet.</p>
  }
  return (
    <nav aria-label="File tree" className="text-sm">
      <NodeList nodes={tree.children} depth={0} />
    </nav>
  )
}
