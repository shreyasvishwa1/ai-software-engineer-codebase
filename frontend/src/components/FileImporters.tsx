import { useCodeImporters } from '../hooks/useCodeImporters'
import { getErrorMessage } from '../services/client'
import type { Importer, ImporterImport, ImportersSummary } from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const NO_IMPORTERS = 'No importers found.'
export const TRUNCATED_NOTICE =
  'The importer list may be incomplete: a scan or result limit was reached (at most 200 importers and 20 statements each).'
export const UNSUPPORTED = 'Importers are only available for Python files.'
export const UNKNOWN_STATUS = 'The importers are unavailable.'
const MAX_LEVEL = 100

interface FileImportersProps {
  projectId: string
  repositoryId: string
  /** The open file's server-provided path. */
  path: string
  language: string | null
  /** When given, every importer path becomes a button that opens that file in the viewer. */
  onSelectFile?: (path: string) => void
}

/**
 * A readable statement assembled only from the returned fields (never source text): Step 41
 * returns no aliases, so none are shown.
 */
export function statementText(entry: ImporterImport): string {
  const names = entry.names.join(', ')
  if (entry.statement === 'import') {
    return names ? `import ${names}` : 'import'
  }
  const level =
    typeof entry.level === 'number' && Number.isInteger(entry.level) && entry.level > 0
      ? Math.min(entry.level, MAX_LEVEL)
      : 0
  const from = `from ${'.'.repeat(level)}${entry.module ?? ''}`
  return names ? `${from} import ${names}` : `${from} import`
}

export function summaryText(summary: ImportersSummary): string {
  return `Scanned ${summary.files_scanned} files · ${summary.too_large} too large · ${summary.binary} binary · ${summary.syntax_error} with syntax errors.`
}

/** One importer: its exact path and every returned statement, in server order. */
function ImporterRow({
  importer,
  onSelectFile,
}: {
  importer: Importer
  onSelectFile?: (path: string) => void
}) {
  return (
    <li className="space-y-1 py-1" data-testid="importer">
      {onSelectFile ? (
        <button
          type="button"
          aria-label={`Open ${importer.path} in the viewer`}
          onClick={() => onSelectFile(importer.path)}
          className="whitespace-pre-wrap break-all text-left font-mono text-indigo-300 hover:underline"
        >
          {importer.path}
        </button>
      ) : (
        <span className="whitespace-pre-wrap break-all font-mono text-slate-200">{importer.path}</span>
      )}
      <ul className="ml-4 space-y-0.5" aria-label={`Statements of ${importer.path}`}>
        {importer.imports.map((entry, index) => (
          <li key={`${index}:${entry.line}`} className="break-all text-slate-400" data-testid="importer-statement">
            {`line ${entry.line} · ${statementText(entry)} · ${entry.resolution}`}
          </li>
        ))}
      </ul>
    </li>
  )
}

/**
 * On-demand "Imported by" list of the displayed Python file. Nothing is requested until the
 * button is clicked; the component is keyed by path, so another file always starts fresh.
 * The server's result is shown as returned: never sorted, filtered or re-resolved.
 */
export function FileImporters({
  projectId,
  repositoryId,
  path,
  language,
  onSelectFile,
}: FileImportersProps) {
  const importers = useCodeImporters(projectId, repositoryId)
  if (language !== 'Python') {
    return null
  }
  const result = importers.isSuccess ? importers.data : undefined
  const label = importers.isPending
    ? 'Loading importers…'
    : result
      ? 'Refresh importers'
      : importers.isError
        ? 'Retry'
        : 'Show importers'
  return (
    <section className="space-y-2 rounded-lg border border-slate-800 p-3" aria-label="File importers">
      <h3 className="text-sm font-semibold text-slate-200">Imported by</h3>
      <button
        type="button"
        disabled={importers.isPending}
        onClick={() => importers.mutate(path)}
        className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {label}
      </button>
      {importers.isError && (
        <ErrorMessage message={getErrorMessage(importers.error, 'Could not load the importers')} />
      )}
      {result && result.status === 'unsupported_language' && <p className={NOTE}>{UNSUPPORTED}</p>}
      {result && result.status !== 'scanned' && result.status !== 'unsupported_language' && (
        <p className={NOTE}>{UNKNOWN_STATUS}</p>
      )}
      {result && result.status === 'scanned' && (
        <>
          {result.truncated === true && <p className={NOTE}>{TRUNCATED_NOTICE}</p>}
          {result.importers.length === 0 ? (
            <p className={NOTE}>{NO_IMPORTERS}</p>
          ) : (
            // Server order (by path; statements in source order); never re-sorted here.
            <ul className="divide-y divide-slate-800 text-xs" aria-label="Importers">
              {result.importers.map((importer, index) => (
                <ImporterRow key={`${index}:${importer.path}`} importer={importer} onSelectFile={onSelectFile} />
              ))}
            </ul>
          )}
          <p className="text-xs text-slate-500" data-testid="importers-summary">
            {summaryText(result.summary)}
          </p>
        </>
      )}
    </section>
  )
}
