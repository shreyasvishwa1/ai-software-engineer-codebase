import { useMemo, useState } from 'react'

import type { TreeDirectory, TreeFile, TreeNode } from '../types/api'
import { formatBytes } from '../utils/format'

export const FILTER_LABEL = 'Filter files by path'
export const NO_MATCHES = 'No files match the filter.'
/** At most this many matching files are listed; the count above them is always exact. */
export const MAX_MATCHES = 100

/** "1 matching file", "3 matching files", or the no-match message. */
export function matchCount(count: number): string {
  return count === 0 ? NO_MATCHES : `${count} matching ${count === 1 ? 'file' : 'files'}`
}

export function capNotice(count: number): string {
  return `Showing the first ${MAX_MATCHES} of ${count} matching files.`
}

/** Every file of the tree in display (server) order, depth first. */
function treeFiles(directory: TreeDirectory, files: TreeFile[] = []): TreeFile[] {
  for (const node of directory.children) {
    if (node.type === 'directory') {
      treeFiles(node, files)
    } else {
      files.push(node)
    }
  }
  return files
}

/**
 * Files whose server path contains the filter as literal text, ignoring case and surrounding
 * spaces; none for a blank filter. Order is kept.
 */
function matchingFiles(files: TreeFile[], filter: string): TreeFile[] {
  const needle = filter.trim().toLowerCase()
  return needle === '' ? [] : files.filter((file) => file.path.toLowerCase().includes(needle))
}

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

function FileDetails({ node, label }: { node: TreeFile; label: string }) {
  return (
    <>
      <span>{label}</span>
      <span className="text-xs text-slate-500">{node.language ?? 'Unknown'}</span>
      <span className="ml-auto text-xs text-slate-500">{formatBytes(node.size_bytes)}</span>
    </>
  )
}

/** One file row, labelled by its name in the tree and by its full path among filter matches. */
function FileNode({
  node,
  selection,
  label = node.name,
}: {
  node: TreeFile
  selection: Selection
  label?: string
}) {
  const { onSelectFile, selectedPath } = selection
  if (!onSelectFile) {
    return (
      <li className="flex items-center gap-2 px-1 py-0.5 text-slate-300">
        <FileDetails node={node} label={label} />
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
        <FileDetails node={node} label={label} />
      </button>
    </li>
  )
}

function NodeList({
  nodes,
  depth,
  selection,
  hidden = false,
}: {
  nodes: TreeNode[]
  depth: number
  selection: Selection
  hidden?: boolean
}) {
  return (
    <ul className={depth > 0 ? 'ml-4 border-l border-slate-800 pl-2' : ''} role="group" hidden={hidden}>
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

/** Matching files as a flat list of full paths, in tree order, at most MAX_MATCHES of them. */
function FilterMatches({ matches, selection }: { matches: TreeFile[]; selection: Selection }) {
  return (
    <div className="mt-1 space-y-1">
      {matches.length > MAX_MATCHES && (
        <p className="text-xs text-slate-500">{capNotice(matches.length)}</p>
      )}
      {matches.length > 0 && (
        <ul aria-label="Matching files">
          {matches.slice(0, MAX_MATCHES).map((node) => (
            <FileNode key={`f:${node.path}`} node={node} selection={selection} label={node.path} />
          ))}
        </ul>
      )}
    </div>
  )
}

/**
 * Collapsible repository tree: top-level entries visible, nested directories collapsed. A filter
 * lists the files whose path contains the typed text, entirely in the browser (nothing is
 * requested or stored); the tree stays mounted while it is hidden, so its expansion survives.
 */
export function FileTree({
  tree,
  onSelectFile,
  selectedPath = null,
}: { tree: TreeDirectory } & Selection) {
  const [filter, setFilter] = useState('')
  const files = useMemo(() => treeFiles(tree), [tree])
  if (tree.children.length === 0) {
    return <p className="text-sm text-slate-400">No files have been ingested yet.</p>
  }
  const selection = { onSelectFile, selectedPath }
  const filtering = filter.trim() !== ''
  const matches = matchingFiles(files, filter)
  return (
    <nav aria-label="File tree" className="text-sm">
      <div className="mb-2 flex items-center gap-2">
        <input
          type="search"
          aria-label={FILTER_LABEL}
          placeholder={FILTER_LABEL}
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              setFilter('')
            }
          }}
          className="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-xs"
        />
        {filter !== '' && (
          <button
            type="button"
            aria-label="Clear the file filter"
            onClick={() => setFilter('')}
            className="rounded-lg border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-800"
          >
            Clear
          </button>
        )}
      </div>
      {/* Always mounted, so screen readers announce each new count. */}
      <p className="text-xs text-slate-500" aria-live="polite" data-testid="file-filter-count">
        {filtering ? matchCount(matches.length) : ''}
      </p>
      {filtering && <FilterMatches matches={matches} selection={selection} />}
      <NodeList nodes={tree.children} depth={0} selection={selection} hidden={filtering} />
    </nav>
  )
}
