import { isAxiosError } from 'axios'
import { useEffect, useRef } from 'react'

import { useFileContent } from '../hooks/useFileContent'
import { getErrorMessage } from '../services/client'
import { formatBytes } from '../utils/format'
import { ErrorMessage } from './ErrorMessage'
import { FileExplanation } from './FileExplanation'
import { FileImporters } from './FileImporters'
import { FileImports } from './FileImports'
import { FileOutline } from './FileOutline'

/** A line to highlight; a new object for every selection. */
export interface LineSelection {
  line: number
}

interface FileViewerProps {
  projectId: string
  repositoryId: string
  selectedPath: string | null
  /** 1-based line reported by an analysis panel; ignored unless a positive integer. */
  selectedLine?: LineSelection | null
  /** Selects a line of the open file (used by the outline). */
  onSelectLine?: (path: string, line: number) => void
  /** Opens another repository file (used by the imports' targets). */
  onSelectFile?: (path: string) => void
}

const NOTE = 'text-sm text-slate-400'

/**
 * (before, line, after) of 1-based `line`, counting lines separated by "\n" like the
 * server does; null when the content has no such line.
 */
function splitAtLine(content: string, line: number): [string, string, string] | null {
  let start = 0
  for (let n = 1; n < line; n++) {
    const next = content.indexOf('\n', start)
    if (next === -1) {
      return null
    }
    start = next + 1
  }
  const end = content.indexOf('\n', start)
  const stop = end === -1 ? content.length : end
  return [content.slice(0, start), content.slice(start, stop), content.slice(stop)]
}

/**
 * Read-only view of one repository file. Content is untrusted: it is rendered only as React
 * text inside <pre><code>, never interpreted as HTML or markup.
 */
export function FileViewer({
  projectId,
  repositoryId,
  selectedPath,
  selectedLine = null,
  onSelectLine,
  onSelectFile,
}: FileViewerProps) {
  const file = useFileContent(projectId, repositoryId, selectedPath)
  const preRef = useRef<HTMLPreElement>(null)
  const lineRef = useRef<HTMLSpanElement>(null)

  // Scroll only the code box (never the page) so the selected line sits near the top third.
  useEffect(() => {
    const pre = preRef.current
    const span = lineRef.current
    if (pre && span) {
      pre.scrollTop = Math.max(0, span.offsetTop - pre.offsetTop - pre.clientHeight / 3)
    }
  }, [selectedLine, file.data])

  if (!selectedPath) {
    return <p className={NOTE}>Select a file to view its content.</p>
  }
  if (file.isPending) {
    return <p className={NOTE}>Loading file…</p>
  }
  if (file.isError) {
    const conflict = isAxiosError(file.error) && file.error.response?.status === 409
    return (
      <div className="space-y-2">
        <p className="break-all font-mono text-sm text-slate-200">{selectedPath}</p>
        <ErrorMessage message={getErrorMessage(file.error, 'Could not load the file')} />
        {conflict && (
          <button
            type="button"
            onClick={() => void file.refetch()}
            className="rounded-lg border border-slate-700 px-3 py-1 text-sm text-slate-200 hover:bg-slate-800"
          >
            Retry
          </button>
        )}
      </div>
    )
  }

  const data = file.data
  let body
  if (data.reason === 'binary') {
    body = <p className={NOTE}>Binary file — preview not available.</p>
  } else if (data.reason === 'too_large') {
    body = <p className={NOTE}>File is too large to preview (limit 1 MiB).</p>
  } else if (!data.content) {
    body = <p className={NOTE}>This file is empty.</p>
  } else {
    const line = selectedLine?.line
    const valid = typeof line === 'number' && Number.isInteger(line) && line >= 1
    // Three pieces at most, whatever the file size; the text stays exactly the content.
    const pieces = valid ? splitAtLine(data.content, line) : null
    body = (
      <>
        {/* Keyed by path: switching files always starts a fresh explanation. */}
        <FileExplanation
          key={data.path}
          projectId={projectId}
          repositoryId={repositoryId}
          path={data.path}
        />
        {/* Keyed by path too: another file never shows this file's outline. */}
        <FileOutline
          key={`outline:${data.path}`}
          projectId={projectId}
          repositoryId={repositoryId}
          path={data.path}
          language={data.language}
          onSelectLine={onSelectLine}
        />
        {/* Keyed by path as well: another file never shows this file's imports. */}
        <FileImports
          key={`imports:${data.path}`}
          projectId={projectId}
          repositoryId={repositoryId}
          path={data.path}
          language={data.language}
          onSelectFile={onSelectFile}
        />
        {/* Keyed by path too: another file never shows this file's importers. */}
        <FileImporters
          key={`importers:${data.path}`}
          projectId={projectId}
          repositoryId={repositoryId}
          path={data.path}
          language={data.language}
          onSelectFile={onSelectFile}
        />
        {pieces && <p className={NOTE}>{`Showing line ${line}`}</p>}
        {valid && !pieces && <p className={NOTE}>{`Line ${line} is not in this file.`}</p>}
        <pre
          ref={preRef}
          className="max-h-[32rem] overflow-auto rounded-lg bg-slate-950 p-3 text-xs leading-5 text-slate-200"
        >
          {pieces ? (
            <code>
              {pieces[0]}
              <span ref={lineRef} data-testid="selected-line" className="bg-amber-400/20">
                {pieces[1]}
              </span>
              {pieces[2]}
            </code>
          ) : (
            <code>{data.content}</code>
          )}
        </pre>
      </>
    )
  }
  return (
    <div className="space-y-3">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1" data-testid="file-header">
        <span className="break-all font-mono text-sm text-slate-200">{data.path}</span>
        <span className="text-xs text-slate-500">{data.language ?? 'Unknown'}</span>
        <span className="text-xs text-slate-500">{formatBytes(data.size_bytes)}</span>
      </header>
      {body}
    </div>
  )
}
