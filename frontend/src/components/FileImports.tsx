import { useCodeImports } from '../hooks/useCodeImports'
import { getErrorMessage } from '../services/client'
import type { ImportEntry, OutlineStatus } from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const NO_IMPORTS = 'No imports found.'
export const TRUNCATED_NOTICE =
  'The import list is incomplete: at most 500 imports, 50 names and 5 targets per import are shown.'
export const UNKNOWN_STATUS = 'The imports are unavailable.'
export const STATUS_MESSAGES: Record<Exclude<OutlineStatus, 'parsed'>, string> = {
  syntax_error: 'The imports are unavailable: this file could not be parsed as Python.',
  too_large: 'The imports are unavailable: this file is too large to analyze (limit 256 KiB).',
  binary: 'The imports are unavailable: this file is not text.',
  unsupported_language: 'Imports are only available for Python files.',
}

interface FileImportsProps {
  projectId: string
  repositoryId: string
  /** The open file's server-provided path. */
  path: string
  language: string | null
  /** When given, every target becomes a button that opens that file in the viewer. */
  onSelectFile?: (path: string) => void
}

/** One import statement: only its named fields, always as plain text, in server order. */
function ImportRow({
  entry,
  onSelectFile,
}: {
  entry: ImportEntry
  onSelectFile?: (path: string) => void
}) {
  return (
    <li className="space-y-1 py-1" data-testid="import-entry">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-slate-500">{`line ${entry.line}`}</span>
        <span className="text-slate-500" data-testid="import-statement">
          {entry.statement}
        </span>
        {entry.module !== null && (
          <span className="break-all font-mono text-slate-200" data-testid="import-module">
            {entry.module}
          </span>
        )}
        <span className="break-all font-mono text-slate-300" data-testid="import-names">
          {entry.names.join(', ')}
        </span>
        <span className="ml-auto text-slate-400" data-testid="import-resolution">
          {entry.resolution}
        </span>
      </div>
      {entry.targets.length > 0 && (
        // Every server target, in server order; never one chosen automatically.
        <ul className="ml-4 space-y-0.5" aria-label={`Targets of line ${entry.line}`}>
          {entry.targets.map((target, index) => (
            <li key={`${index}:${target}`}>
              {onSelectFile ? (
                <button
                  type="button"
                  aria-label={`Open ${target} in the viewer`}
                  onClick={() => onSelectFile(target)}
                  className="break-all text-left font-mono text-indigo-300 hover:underline"
                >
                  {target}
                </button>
              ) : (
                <span className="break-all font-mono text-slate-300">{target}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

/**
 * On-demand resolved imports of the displayed Python file. Nothing is requested until the
 * button is clicked; the component is keyed by path, so another file always starts fresh.
 * The server's resolution is shown as returned: never re-resolved, sorted or filtered.
 */
export function FileImports({
  projectId,
  repositoryId,
  path,
  language,
  onSelectFile,
}: FileImportsProps) {
  const imports = useCodeImports(projectId, repositoryId)
  if (language !== 'Python') {
    return null
  }
  const result = imports.isSuccess ? imports.data : undefined
  const unavailable =
    result && result.status !== 'parsed'
      ? (STATUS_MESSAGES[result.status as keyof typeof STATUS_MESSAGES] ?? UNKNOWN_STATUS)
      : null
  const label = imports.isPending
    ? 'Loading imports…'
    : result
      ? 'Refresh imports'
      : imports.isError
        ? 'Retry'
        : 'Show imports'
  return (
    <section className="space-y-2 rounded-lg border border-slate-800 p-3" aria-label="File imports">
      <button
        type="button"
        disabled={imports.isPending}
        onClick={() => imports.mutate(path)}
        className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {label}
      </button>
      {imports.isError && (
        <ErrorMessage message={getErrorMessage(imports.error, 'Could not load the imports')} />
      )}
      {unavailable && <p className={NOTE}>{unavailable}</p>}
      {result && result.status === 'parsed' && (
        <>
          {result.truncated === true && <p className={NOTE}>{TRUNCATED_NOTICE}</p>}
          {result.imports.length === 0 ? (
            <p className={NOTE}>{NO_IMPORTS}</p>
          ) : (
            <ul className="divide-y divide-slate-800 text-xs" aria-label="Imports">
              {result.imports.map((entry, index) => (
                <ImportRow key={`${index}:${entry.line}`} entry={entry} onSelectFile={onSelectFile} />
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  )
}
