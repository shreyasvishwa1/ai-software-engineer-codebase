import { useCodeUnusedImports } from '../hooks/useCodeUnusedImports'
import { getErrorMessage } from '../services/client'
import type { OutlineStatus, UnusedImport, UnusedImportsSummary } from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const HEADING = 'Imported names that are never read'
export const EMPTY_RESULT = 'No unused imports found.'
export const TRUNCATED_NOTICE = 'Unused import results are truncated: only the first 500 are shown.'
export const UNKNOWN_STATUS = 'Unused imports are unavailable.'
export const STATUS_MESSAGES: Record<Exclude<OutlineStatus, 'parsed'>, string> = {
  syntax_error: 'Unused imports are unavailable: this file could not be parsed as Python.',
  too_large: 'Unused imports are unavailable: this file is too large to analyze (limit 256 KiB).',
  binary: 'Unused imports are unavailable: this file is not text.',
  unsupported_language: 'Unused imports are only available for Python files.',
}

/** "1 import", "3 imports": the noun is singular only for exactly one. */
function counted(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`
}

/** The server's counts, never derived from the rows shown (they are counted before the cap). */
export function unusedImportsSummary(summary: UnusedImportsSummary): string {
  return `Unused: ${counted(summary.unused, 'import')} of ${counted(summary.distinct, 'name')}.`
}

/** The server's count of never-read imports it does not list; the reasons are the server's. */
export function unsupportedImportsNote(unsupported: number): string {
  return `Not listed: ${counted(unsupported, 'import')} possibly used through a package or stub interface, dynamic access or a string annotation.`
}

type Navigate = (path: string, line: number) => void

/** Every import in server (source) order; the server's path is the file they are in. */
function Imports({
  imports,
  filePath,
  onSelectLine,
}: {
  imports: UnusedImport[]
  filePath: string
  onSelectLine?: Navigate
}) {
  return (
    // Never re-sorted, deduplicated or regrouped here; names repeat, so keys use positions.
    <ul className="space-y-0.5" aria-label="Unused imported names">
      {imports.map((entry, index) => {
        const text = `${entry.name} line ${entry.line}, column ${entry.column}`
        return (
          <li key={`${index}:${entry.line}:${entry.column}`} data-testid="unused-import">
            {onSelectLine ? (
              <button
                type="button"
                aria-label={`Go to ${entry.name} at line ${entry.line}, column ${entry.column}`}
                onClick={() => onSelectLine(filePath, entry.line)}
                className="text-left font-mono text-slate-300 hover:underline"
              >
                {text}
              </button>
            ) : (
              <span className="font-mono text-slate-300">{text}</span>
            )}
          </li>
        )
      })}
    </ul>
  )
}

interface FileUnusedImportsProps {
  projectId: string
  repositoryId: string
  /** The open file's server-provided path. */
  path: string
  language: string | null
  /** When given, every import becomes a button that selects its line in the viewer. */
  onSelectLine?: Navigate
}

/**
 * On-demand unused imports of the displayed Python file: imports whose bound name nothing in the
 * file reads, as found by the server. Nothing is requested until the button is clicked; the
 * component is keyed by path, so another file always starts fresh. The server's entries, counts
 * and flags are shown as returned, always as plain text.
 */
export function FileUnusedImports({
  projectId,
  repositoryId,
  path,
  language,
  onSelectLine,
}: FileUnusedImportsProps) {
  const unused = useCodeUnusedImports(projectId, repositoryId)
  if (language !== 'Python') {
    return null
  }
  const result = unused.isSuccess ? unused.data : undefined
  const unavailable =
    result && result.status !== 'parsed'
      ? (STATUS_MESSAGES[result.status as keyof typeof STATUS_MESSAGES] ?? UNKNOWN_STATUS)
      : null
  const label = unused.isPending
    ? 'Finding unused imports…'
    : result
      ? 'Refresh unused imports'
      : 'Find unused imports'
  return (
    <section className="space-y-2 rounded-lg border border-slate-800 p-3" aria-label="Unused imports">
      <button
        type="button"
        disabled={unused.isPending}
        onClick={() => unused.mutate(path)}
        className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {label}
      </button>
      {unused.isError && (
        <ErrorMessage message={getErrorMessage(unused.error, 'Could not check unused imports')} />
      )}
      {unavailable && <p className={NOTE}>{unavailable}</p>}
      {result && result.status === 'parsed' && (
        <div className="space-y-2 text-xs">
          <h4 className="font-semibold text-slate-300">{HEADING}</h4>
          <p className="text-slate-500" data-testid="unused-imports-summary">
            {unusedImportsSummary(result.summary)}
          </p>
          {result.summary.unsupported > 0 && (
            <p className={NOTE}>{unsupportedImportsNote(result.summary.unsupported)}</p>
          )}
          {result.truncated === true && <p className={NOTE}>{TRUNCATED_NOTICE}</p>}
          {result.imports.length === 0 ? (
            <p className={NOTE}>{EMPTY_RESULT}</p>
          ) : (
            <Imports imports={result.imports} filePath={result.path} onSelectLine={onSelectLine} />
          )}
        </div>
      )}
    </section>
  )
}
