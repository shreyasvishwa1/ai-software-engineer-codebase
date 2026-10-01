import { useState } from 'react'

import type { TreeDirectory, TreeFile, TreeNode } from '../types/api'
import { formatBytes } from '../utils/format'

/** Optional file selection; without onSelectFile, file rows are not interactive. */
interface Selection {
  onSelectFile?: (path: string) => void
  selectedPath?: string | null
}

function DirectoryNode({
  node,
  depth,
  selection,
}: {
  node: TreeDirectory
  depth: number
  selection: Selection
}) {
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
      {open && <NodeList nodes={node.children} depth={depth + 1} selection={selection} />}
    </li>
  )
}

function FileDetails({ node }: { node: TreeFile }) {
  return (
    <>
      <span>{node.name}</span>
      <span className="text-xs text-slate-500">{node.language ?? 'Unknown'}</span>
      <span className="ml-auto text-xs text-slate-500">{formatBytes(node.size_bytes)}</span>
    </>
  )
}

function FileNode({ node, selection }: { node: TreeFile; selection: Selection }) {
  const { onSelectFile, selectedPath } = selection
  if (!onSelectFile) {
    return (
      <li className="flex items-center gap-2 px-1 py-0.5 text-slate-300">
        <FileDetails node={node} />
      </li>
    )
  }
  const selected = selectedPath === node.path
  return (
    <li>
      <button
        type="button"
        aria-current={selected ? 'true' : undefined}
        onClick={() => onSelectFile(node.path)}
        className={`flex w-full items-center gap-2 rounded px-1 py-0.5 text-left text-slate-300 hover:bg-slate-800 ${
          selected ? 'bg-slate-800 text-slate-100' : ''
        }`}
      >
        <FileDetails node={node} />
      </button>
    </li>
  )
}

function NodeList({
  nodes,
  depth,
  selection,
}: {
  nodes: TreeNode[]
  depth: number
  selection: Selection
}) {
  return (
    <ul className={depth > 0 ? 'ml-4 border-l border-slate-800 pl-2' : ''} role="group">
      {nodes.map((node) =>
        node.type === 'directory' ? (
          <DirectoryNode key={`d:${node.path}`} node={node} depth={depth} selection={selection} />
        ) : (
          <FileNode key={`f:${node.path}`} node={node} selection={selection} />
        ),
      )}
    </ul>
  )
}

/** Collapsible repository tree: top-level entries visible, nested directories collapsed. */
export function FileTree({
  tree,
  onSelectFile,
  selectedPath = null,
}: { tree: TreeDirectory } & Selection) {
  if (tree.children.length === 0) {
    return <p className="text-sm text-slate-400">No files have been ingested yet.</p>
  }
  return (
    <nav aria-label="File tree" className="text-sm">
      <NodeList nodes={tree.children} depth={0} selection={{ onSelectFile, selectedPath }} />
    </nav>
  )
}
