import { useCodeUnreachableCode } from '../hooks/useCodeUnreachableCode'
import { getErrorMessage } from '../services/client'
import type { OutlineStatus, UnreachableCodeSummary, UnreachableRegion } from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const HEADING = 'Statements that can never run'
export const EMPTY_RESULT = 'No unreachable code found.'
export const TRUNCATED_NOTICE = 'Unreachable code results are truncated: only the first 500 regions are shown.'
export const UNKNOWN_STATUS = 'Unreachable code is unavailable.'
export const STATUS_MESSAGES: Record<Exclude<OutlineStatus, 'parsed'>, string> = {
  syntax_error: 'Unreachable code is unavailable: this file could not be parsed as Python.',
  too_large: 'Unreachable code is unavailable: this file is too large to analyze (limit 256 KiB).',
  binary: 'Unreachable code is unavailable: this file is not text.',
  unsupported_language: 'Unreachable code is only available for Python files.',
}
/** What makes each region unreachable, as the server names it. */
export const CAUSES: Record<UnreachableRegion['cause'], string> = {
  return: 'after a return',
  raise: 'after a raise',
  break: 'after a break',
  continue: 'after a continue',
  if: 'after an if statement whose every branch leaves',
  while: 'after a loop that never ends',
}
export const UNKNOWN_CAUSE = 'after a statement that always leaves'

/** "1 statement", "3 statements": the noun is singular only for exactly one. */
function counted(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`
}

/** The server's counts, never derived from the rows shown (they are counted before the cap). */
export function unreachableSummary(summary: UnreachableCodeSummary): string {
  return `Never run: ${counted(summary.statements, 'statement')} in ${counted(summary.regions, 'region')}.`
}

/** "line 5" or "lines 5–7", exactly as the server bounds the region. */
export function lineRange(region: UnreachableRegion): string {
  return region.line === region.end_line ? `line ${region.line}` : `lines ${region.line}–${region.end_line}`
}

type Navigate = (path: string, line: number) => void

/** Every region in server (source) order; the server's path is the file they are in. */
function Regions({
  regions,
  filePath,
  onSelectLine,
}: {
  regions: UnreachableRegion[]
  filePath: string
  onSelectLine?: Navigate
}) {
  return (
    // Never re-sorted, merged or regrouped here; keys use positions.
    <ul className="space-y-0.5" aria-label="Unreachable regions">
      {regions.map((region, index) => {
        const range = lineRange(region)
        const text = `${range} (${counted(region.statements, 'statement')})`
        const cause = `${CAUSES[region.cause] ?? UNKNOWN_CAUSE} on line ${region.cause_line}`
        return (
          <li key={`${index}:${region.line}`} data-testid="unreachable-region">
            {onSelectLine ? (
              <>
                <button
                  type="button"
                  aria-label={`Go to the unreachable code at ${range}`}
                  onClick={() => onSelectLine(filePath, region.line)}
                  className="text-left font-mono text-slate-300 hover:underline"
                >
                  {text}
                </button>
                {' '}
                <button
                  type="button"
                  aria-label={`Go to the statement that makes ${range} unreachable, on line ${region.cause_line}`}
                  onClick={() => onSelectLine(filePath, region.cause_line)}
                  className="text-left text-slate-400 hover:underline"
                >
                  {cause}
                </button>
              </>
            ) : (
              <span className="font-mono text-slate-300">{`${text} ${cause}`}</span>
            )}
          </li>
        )
      })}
    </ul>
  )
}

interface FileUnreachableCodeProps {
  projectId: string
  repositoryId: string
  /** The open file's server-provided path. */
  path: string
  language: string | null
  /** When given, every region and its cause become buttons that select their lines. */
  onSelectLine?: Navigate
}

/**
 * On-demand unreachable code of the displayed Python file: statements after a statement that
 * always leaves its block, as found by the server. Nothing is requested until the button is
 * clicked; the component is keyed by path, so another file always starts fresh. The server's
 * regions, counts and flags are shown as returned, always as plain text.
 */
export function FileUnreachableCode({
  projectId,
  repositoryId,
  path,
  language,
  onSelectLine,
}: FileUnreachableCodeProps) {
  const unreachable = useCodeUnreachableCode(projectId, repositoryId)
  if (language !== 'Python') {
    return null
  }
  const result = unreachable.isSuccess ? unreachable.data : undefined
  const unavailable =
    result && result.status !== 'parsed'
      ? (STATUS_MESSAGES[result.status as keyof typeof STATUS_MESSAGES] ?? UNKNOWN_STATUS)
      : null
  const label = unreachable.isPending
    ? 'Finding unreachable code…'
    : result
      ? 'Refresh unreachable code'
      : 'Find unreachable code'
  return (
    <section className="space-y-2 rounded-lg border border-slate-800 p-3" aria-label="Unreachable code">
      <button
        type="button"
        disabled={unreachable.isPending}
        onClick={() => unreachable.mutate(path)}
        className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {label}
      </button>
      {unreachable.isError && (
        <ErrorMessage message={getErrorMessage(unreachable.error, 'Could not check unreachable code')} />
      )}
      {unavailable && <p className={NOTE}>{unavailable}</p>}
      {result && result.status === 'parsed' && (
        <div className="space-y-2 text-xs">
          <h4 className="font-semibold text-slate-300">{HEADING}</h4>
          <p className="text-slate-500" data-testid="unreachable-summary">
            {unreachableSummary(result.summary)}
          </p>
          {result.truncated === true && <p className={NOTE}>{TRUNCATED_NOTICE}</p>}
          {result.regions.length === 0 ? (
            <p className={NOTE}>{EMPTY_RESULT}</p>
          ) : (
            <Regions regions={result.regions} filePath={result.path} onSelectLine={onSelectLine} />
          )}
        </div>
      )}
    </section>
  )
}
