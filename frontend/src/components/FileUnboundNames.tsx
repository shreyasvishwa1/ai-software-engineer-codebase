import { useCodeUnboundNames } from '../hooks/useCodeUnboundNames'
import { getErrorMessage } from '../services/client'
import type { OutlineStatus, UnboundName, UnboundNamesSummary } from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const HEADING = 'Names with no statically known binding'
export const EMPTY_RESULT = 'No unbound names found.'
export const TRUNCATED_NOTICE = 'Unbound name results are truncated.'
export const UNKNOWN_STATUS = 'Unbound names are unavailable.'
export const STATUS_MESSAGES: Record<Exclude<OutlineStatus, 'parsed'>, string> = {
  syntax_error: 'Unbound names are unavailable: this file could not be parsed as Python.',
  too_large: 'Unbound names are unavailable: this file is too large to analyze (limit 256 KiB).',
  binary: 'Unbound names are unavailable: this file is not text.',
  unsupported_language: 'Unbound names are only available for Python files.',
}

/** The server's counts, never derived from the rows shown (they are counted before the cap). */
export function unboundSummary(summary: UnboundNamesSummary): string {
  return `Unbound: ${summary.unbound} occurrences of ${summary.distinct} names.`
}

export function starImportNote(unsupported: number): string {
  return `${unsupported} names may come from a star import and are not listed.`
}

type Navigate = (path: string, line: number) => void

/** Every occurrence in server (source) order; the server's path is the file they are in. */
function Occurrences({
  names,
  filePath,
  onSelectLine,
}: {
  names: UnboundName[]
  filePath: string
  onSelectLine?: Navigate
}) {
  return (
    // Never re-sorted, deduplicated or regrouped here; names repeat, so keys use positions.
    <ul className="space-y-0.5" aria-label="Unbound name occurrences">
      {names.map((occurrence, index) => {
        const text = `${occurrence.name} line ${occurrence.line}, column ${occurrence.column}`
        return (
          <li key={`${index}:${occurrence.line}:${occurrence.column}`} data-testid="unbound-name">
            {onSelectLine ? (
              <button
                type="button"
                aria-label={`Go to ${occurrence.name} at line ${occurrence.line}, column ${occurrence.column}`}
                onClick={() => onSelectLine(filePath, occurrence.line)}
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

interface FileUnboundNamesProps {
  projectId: string
  repositoryId: string
  /** The open file's server-provided path. */
  path: string
  language: string | null
  /** When given, every occurrence becomes a button that selects its line in the viewer. */
  onSelectLine?: Navigate
}

/**
 * On-demand statically unbound names of the displayed Python file: identifier loads with no
 * statically known binding (not a runtime error detector). Nothing is requested until the
 * button is clicked; the component is keyed by path, so another file always starts fresh.
 * The server's occurrences, counts and flags are shown as returned, always as plain text.
 */
export function FileUnboundNames({
  projectId,
  repositoryId,
  path,
  language,
  onSelectLine,
}: FileUnboundNamesProps) {
  const unbound = useCodeUnboundNames(projectId, repositoryId)
  if (language !== 'Python') {
    return null
  }
  const result = unbound.isSuccess ? unbound.data : undefined
  const unavailable =
    result && result.status !== 'parsed'
      ? (STATUS_MESSAGES[result.status as keyof typeof STATUS_MESSAGES] ?? UNKNOWN_STATUS)
      : null
  const label = unbound.isPending
    ? 'Finding unbound names…'
    : result
      ? 'Refresh unbound names'
      : 'Find unbound names'
  return (
    <section className="space-y-2 rounded-lg border border-slate-800 p-3" aria-label="Unbound names">
      <button
        type="button"
        disabled={unbound.isPending}
        onClick={() => unbound.mutate(path)}
        className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {label}
      </button>
      {unbound.isError && (
        <ErrorMessage message={getErrorMessage(unbound.error, 'Could not check unbound names')} />
      )}
      {unavailable && <p className={NOTE}>{unavailable}</p>}
      {result && result.status === 'parsed' && (
        <div className="space-y-2 text-xs">
          <h4 className="font-semibold text-slate-300">{HEADING}</h4>
          <p className="text-slate-500" data-testid="unbound-summary">
            {unboundSummary(result.summary)}
          </p>
          {result.summary.unsupported > 0 && (
            <p className={NOTE}>{starImportNote(result.summary.unsupported)}</p>
          )}
          {result.truncated === true && <p className={NOTE}>{TRUNCATED_NOTICE}</p>}
          {result.names.length === 0 ? (
            <p className={NOTE}>{EMPTY_RESULT}</p>
          ) : (
            <Occurrences names={result.names} filePath={result.path} onSelectLine={onSelectLine} />
          )}
        </div>
      )}
    </section>
  )
}
