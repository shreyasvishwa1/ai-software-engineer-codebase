import { useCodeUnusedVariables } from '../hooks/useCodeUnusedVariables'
import { getErrorMessage } from '../services/client'
import type { OutlineStatus, UnusedVariable, UnusedVariablesSummary } from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const HEADING = 'Local variables that are never read'
export const EMPTY_RESULT = 'No unused variables found.'
export const TRUNCATED_NOTICE = 'Unused variable results are truncated: only the first 500 are shown.'
export const UNKNOWN_STATUS = 'Unused variables are unavailable.'
export const STATUS_MESSAGES: Record<Exclude<OutlineStatus, 'parsed'>, string> = {
  syntax_error: 'Unused variables are unavailable: this file could not be parsed as Python.',
  too_large: 'Unused variables are unavailable: this file is too large to analyze (limit 256 KiB).',
  binary: 'Unused variables are unavailable: this file is not text.',
  unsupported_language: 'Unused variables are only available for Python files.',
}

/** "1 assignment", "3 assignments": the noun is singular only for exactly one. */
function counted(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`
}

/** The server's counts, never derived from the rows shown (they are counted before the cap). */
export function unusedSummary(summary: UnusedVariablesSummary): string {
  return `Unused: ${counted(summary.unused, 'assignment')} to ${counted(summary.distinct, 'variable')}.`
}

export function unsupportedNote(unsupported: number): string {
  return `Not listed: ${counted(unsupported, 'variable')} possibly read dynamically or through string annotations.`
}

type Navigate = (path: string, line: number) => void

/** Every assignment in server (source) order; the server's path is the file they are in. */
function Assignments({
  variables,
  filePath,
  onSelectLine,
}: {
  variables: UnusedVariable[]
  filePath: string
  onSelectLine?: Navigate
}) {
  return (
    // Never re-sorted, deduplicated or regrouped here; names repeat, so keys use positions.
    <ul className="space-y-0.5" aria-label="Unused variable assignments">
      {variables.map((variable, index) => {
        const text = `${variable.name} line ${variable.line}, column ${variable.column}`
        return (
          <li key={`${index}:${variable.line}:${variable.column}`} data-testid="unused-variable">
            {onSelectLine ? (
              <button
                type="button"
                aria-label={`Go to ${variable.name} at line ${variable.line}, column ${variable.column}`}
                onClick={() => onSelectLine(filePath, variable.line)}
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

interface FileUnusedVariablesProps {
  projectId: string
  repositoryId: string
  /** The open file's server-provided path. */
  path: string
  language: string | null
  /** When given, every assignment becomes a button that selects its line in the viewer. */
  onSelectLine?: Navigate
}

/**
 * On-demand unused local variables of the displayed Python file: assignments to def/async def
 * locals that nothing in the file reads (not a data-flow analysis). Nothing is requested until the
 * button is clicked; the component is keyed by path, so another file always starts fresh. The
 * server's entries, counts and flags are shown as returned, always as plain text.
 */
export function FileUnusedVariables({
  projectId,
  repositoryId,
  path,
  language,
  onSelectLine,
}: FileUnusedVariablesProps) {
  const unused = useCodeUnusedVariables(projectId, repositoryId)
  if (language !== 'Python') {
    return null
  }
  const result = unused.isSuccess ? unused.data : undefined
  const unavailable =
    result && result.status !== 'parsed'
      ? (STATUS_MESSAGES[result.status as keyof typeof STATUS_MESSAGES] ?? UNKNOWN_STATUS)
      : null
  const label = unused.isPending
    ? 'Finding unused variables…'
    : result
      ? 'Refresh unused variables'
      : 'Find unused variables'
  return (
    <section className="space-y-2 rounded-lg border border-slate-800 p-3" aria-label="Unused variables">
      <button
        type="button"
        disabled={unused.isPending}
        onClick={() => unused.mutate(path)}
        className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {label}
      </button>
      {unused.isError && (
        <ErrorMessage message={getErrorMessage(unused.error, 'Could not check unused variables')} />
      )}
      {unavailable && <p className={NOTE}>{unavailable}</p>}
      {result && result.status === 'parsed' && (
        <div className="space-y-2 text-xs">
          <h4 className="font-semibold text-slate-300">{HEADING}</h4>
          <p className="text-slate-500" data-testid="unused-summary">
            {unusedSummary(result.summary)}
          </p>
          {result.summary.unsupported > 0 && (
            <p className={NOTE}>{unsupportedNote(result.summary.unsupported)}</p>
          )}
          {result.truncated === true && <p className={NOTE}>{TRUNCATED_NOTICE}</p>}
          {result.variables.length === 0 ? (
            <p className={NOTE}>{EMPTY_RESULT}</p>
          ) : (
            <Assignments
              variables={result.variables}
              filePath={result.path}
              onSelectLine={onSelectLine}
            />
          )}
        </div>
      )}
    </section>
  )
}
