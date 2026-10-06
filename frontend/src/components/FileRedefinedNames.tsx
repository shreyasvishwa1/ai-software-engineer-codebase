import { useCodeReplacedDefinitions } from '../hooks/useCodeReplacedDefinitions'
import { getErrorMessage } from '../services/client'
import type { OutlineStatus, ReplacedDefinition, ReplacedDefinitionsSummary } from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const HEADING = 'Names redefined before they are read'
export const EMPTY_RESULT = 'No redefined names found.'
export const TRUNCATED_NOTICE = 'Redefined name results are truncated: only the first 500 are shown.'
export const UNKNOWN_STATUS = 'Redefined names are unavailable.'
export const STATUS_MESSAGES: Record<Exclude<OutlineStatus, 'parsed'>, string> = {
  syntax_error: 'Redefined names are unavailable: this file could not be parsed as Python.',
  too_large: 'Redefined names are unavailable: this file is too large to analyze (limit 256 KiB).',
  binary: 'Redefined names are unavailable: this file is not text.',
  unsupported_language: 'Redefined names are only available for Python files.',
}

/** "1 statement", "3 statements": the noun is singular only for exactly one. */
function counted(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`
}

/** The server's counts, never derived from the rows shown (they are counted before the cap). */
export function redefinedNamesSummary(summary: ReplacedDefinitionsSummary): string {
  return `Replaced before any read: ${counted(summary.replaced, 'statement')} for ${counted(summary.distinct, 'name')}.`
}

/** The server's count of replaced statements it does not list; the reasons are the server's. */
export function unsupportedRedefinitionsNote(unsupported: number): string {
  return `Not listed: ${counted(unsupported, 'statement')} replaced later that decorators, class creation or dynamic access may keep in use.`
}

type Navigate = (path: string, line: number) => void

/** Every replaced statement in server (source) order; the server's path is the file they are in. */
function Redefinitions({
  definitions,
  filePath,
  onSelectLine,
}: {
  definitions: ReplacedDefinition[]
  filePath: string
  onSelectLine?: Navigate
}) {
  return (
    // Never re-sorted, deduplicated or regrouped here; names repeat, so keys use positions.
    <ul className="space-y-0.5" aria-label="Names replaced before use">
      {definitions.map((entry, index) => {
        const subject = `${entry.kind} ${entry.name}`
        const text = `${subject} line ${entry.line}, column ${entry.column}`
        const replaced = `replaced on line ${entry.replaced_line}`
        return (
          <li key={`${index}:${entry.line}:${entry.column}`} data-testid="redefined-name">
            {onSelectLine ? (
              <>
                <button
                  type="button"
                  aria-label={`Go to ${subject} at line ${entry.line}, column ${entry.column}`}
                  onClick={() => onSelectLine(filePath, entry.line)}
                  className="text-left font-mono text-slate-300 hover:underline"
                >
                  {text}
                </button>
                {' — '}
                <button
                  type="button"
                  aria-label={`Go to the replacement of ${subject} at line ${entry.replaced_line}, column ${entry.replaced_column}`}
                  onClick={() => onSelectLine(filePath, entry.replaced_line)}
                  className="text-left text-slate-400 hover:underline"
                >
                  {replaced}
                </button>
              </>
            ) : (
              <span className="font-mono text-slate-300">{`${text} — ${replaced}`}</span>
            )}
          </li>
        )
      })}
    </ul>
  )
}

interface FileRedefinedNamesProps {
  projectId: string
  repositoryId: string
  /** The open file's server-provided path. */
  path: string
  language: string | null
  /** When given, every statement and its replacement become buttons that select their lines. */
  onSelectLine?: Navigate
}

/**
 * On-demand redefined names of the displayed Python file: def and class statements that a later one
 * of the same name replaces before anything reads them, as found by the server. Nothing is
 * requested until the button is clicked; the component is keyed by path, so another file always
 * starts fresh. The server's entries, counts and flags are shown as returned, always as plain text.
 */
export function FileRedefinedNames({
  projectId,
  repositoryId,
  path,
  language,
  onSelectLine,
}: FileRedefinedNamesProps) {
  const redefined = useCodeReplacedDefinitions(projectId, repositoryId)
  if (language !== 'Python') {
    return null
  }
  const result = redefined.isSuccess ? redefined.data : undefined
  const unavailable =
    result && result.status !== 'parsed'
      ? (STATUS_MESSAGES[result.status as keyof typeof STATUS_MESSAGES] ?? UNKNOWN_STATUS)
      : null
  const label = redefined.isPending
    ? 'Finding redefined names…'
    : result
      ? 'Refresh redefined names'
      : 'Find redefined names'
  return (
    <section className="space-y-2 rounded-lg border border-slate-800 p-3" aria-label="Redefined names">
      <button
        type="button"
        disabled={redefined.isPending}
        onClick={() => redefined.mutate(path)}
        className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {label}
      </button>
      {redefined.isError && (
        <ErrorMessage message={getErrorMessage(redefined.error, 'Could not check redefined names')} />
      )}
      {unavailable && <p className={NOTE}>{unavailable}</p>}
      {result && result.status === 'parsed' && (
        <div className="space-y-2 text-xs">
          <h4 className="font-semibold text-slate-300">{HEADING}</h4>
          <p className="text-slate-500" data-testid="redefined-names-summary">
            {redefinedNamesSummary(result.summary)}
          </p>
          {result.summary.unsupported > 0 && (
            <p className={NOTE}>{unsupportedRedefinitionsNote(result.summary.unsupported)}</p>
          )}
          {result.truncated === true && <p className={NOTE}>{TRUNCATED_NOTICE}</p>}
          {result.definitions.length === 0 ? (
            <p className={NOTE}>{EMPTY_RESULT}</p>
          ) : (
            <Redefinitions
              definitions={result.definitions}
              filePath={result.path}
              onSelectLine={onSelectLine}
            />
          )}
        </div>
      )}
    </section>
  )
}
