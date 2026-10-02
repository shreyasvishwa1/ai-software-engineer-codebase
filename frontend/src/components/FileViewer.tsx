import { isAxiosError } from 'axios'

import { useFileContent } from '../hooks/useFileContent'
import { getErrorMessage } from '../services/client'
import { formatBytes } from '../utils/format'
import { ErrorMessage } from './ErrorMessage'
import { FileExplanation } from './FileExplanation'

interface FileViewerProps {
  projectId: string
  repositoryId: string
  selectedPath: string | null
}

const NOTE = 'text-sm text-slate-400'

/**
 * Read-only view of one repository file. Content is untrusted: it is rendered only as React
 * text inside <pre><code>, never interpreted as HTML or markup.
 */
export function FileViewer({ projectId, repositoryId, selectedPath }: FileViewerProps) {
  const file = useFileContent(projectId, repositoryId, selectedPath)

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
    body = (
      <>
        {/* Keyed by path: switching files always starts a fresh explanation. */}
        <FileExplanation
          key={data.path}
          projectId={projectId}
          repositoryId={repositoryId}
          path={data.path}
        />
        <pre className="max-h-[32rem] overflow-auto rounded-lg bg-slate-950 p-3 text-xs leading-5 text-slate-200">
          <code>{data.content}</code>
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
