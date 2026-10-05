import { useState } from 'react'

import { useCodeOccurrences } from '../hooks/useCodeOccurrences'
import { getErrorMessage } from '../services/client'
import type { Occurrence, OccurrencesStatus, OccurrencesSummary } from '../types/api'
import { positionAt } from './CodeDefinition'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const NO_POSITION = 'Place the cursor on a name in the code first.'
export const MODULE_NOTE = 'Only this file is searched.'
export const TRUNCATED_NOTICE = 'Occurrence results are truncated: only the first 1,000 are shown.'
export const EMPTY_RESULT = 'No occurrences found.'
export const UNKNOWN_STATUS = 'Name occurrences are unavailable.'
export const STATUS_MESSAGES: Record<Exclude<OccurrencesStatus, 'found'>, string> = {
  not_bound: 'No binding for this name in this file.',
  unsupported: 'Occurrences are not supported for this name.',
  no_identifier: 'No name at this position.',
  syntax_error: 'Name occurrences are unavailable: this file could not be parsed as Python.',
  too_large: 'Name occurrences are unavailable: this file is too large to analyze (limit 256 KiB).',
  binary: 'Name occurrences are unavailable: this file is not text.',
  unsupported_language: 'Name occurrences are only available for Python files.',
}

function count(value: number, singular: string, plural: string): string {
  return `${value} ${value === 1 ? singular : plural}`
}

/** The server's counts, never derived from the rows shown (they are counted before the cap). */
export function summaryText(summary: OccurrencesSummary): string {
  return [
    count(summary.occurrences, 'occurrence', 'occurrences'),
    count(summary.reads, 'read', 'reads'),
    count(summary.writes, 'write', 'writes'),
    count(summary.deletes, 'delete', 'deletes'),
    count(summary.bindings, 'binding', 'bindings'),
  ].join(' · ')
}

/** One occurrence as plain text: its position and kind. */
export function occurrenceLabel(occurrence: Occurrence): string {
  return `Line ${occurrence.line}, column ${occurrence.column}: ${occurrence.kind}`
}

interface CodeOccurrencesProps {
  projectId: string
  repositoryId: string
  /** The open file's server-provided path. */
  path: string
  language: string | null
  /** The open file's text, exactly as displayed. */
  content: string
  /** UTF-16 offset of the current selection in the displayed text, read on click only. */
  getOffset: () => number | null
  /** When given, every occurrence becomes a button that selects its line in the viewer. */
  onSelectLine?: (path: string, line: number) => void
}

/**
 * On-demand occurrences, in the displayed Python file, of the binding of the name at the
 * caret (Step 58). Nothing is requested until the button is clicked, and every click requests
 * afresh; the position comes from the browser's selection at that moment. The component is
 * keyed by path, so another file always starts fresh. The server's occurrences, counts and
 * flags are shown as returned, always as plain text.
 */
export function CodeOccurrences({
  projectId,
  repositoryId,
  path,
  language,
  content,
  getOffset,
  onSelectLine,
}: CodeOccurrencesProps) {
  const occurrences = useCodeOccurrences(projectId, repositoryId)
  const [noPosition, setNoPosition] = useState(false)
  if (language !== 'Python') {
    return null
  }

  const find = () => {
    const offset = getOffset()
    if (offset === null) {
      setNoPosition(true)
      return
    }
    setNoPosition(false)
    occurrences.mutate({ path, position: positionAt(content, offset) })
  }

  const result = occurrences.isSuccess ? occurrences.data : undefined
  const found = result !== undefined && result.status === 'found'
  const unavailable =
    result && !found
      ? (STATUS_MESSAGES[result.status as keyof typeof STATUS_MESSAGES] ?? UNKNOWN_STATUS)
      : null
  // The server's path for the reference's file: the occurrences are all in it.
  const target = result?.reference?.path
  return (
    <section className="space-y-2 rounded-lg border border-slate-800 p-3" aria-label="Name occurrences">
      <button
        type="button"
        disabled={occurrences.isPending}
        // Keep the code selection: the click must not move focus or clear it.
        onMouseDown={(event) => event.preventDefault()}
        onClick={find}
        className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {occurrences.isPending ? 'Finding occurrences…' : 'Find occurrences'}
      </button>
      {noPosition && <p className={NOTE}>{NO_POSITION}</p>}
      {occurrences.isError && (
        <ErrorMessage message={getErrorMessage(occurrences.error, 'Could not find the occurrences')} />
      )}
      {unavailable && <p className={NOTE}>{unavailable}</p>}
      {result && found && (
        <div className="space-y-2 text-xs">
          {result.reference && (
            <p className={NOTE} data-testid="occurrences-subject">
              Occurrences of <span className="font-mono text-slate-200">{result.reference.name}</span>
              {result.scope ? ` (${result.scope} scope)` : ''}
            </p>
          )}
          {result.scope === 'module' && <p className={NOTE}>{MODULE_NOTE}</p>}
          <p className="text-slate-500" data-testid="occurrences-summary">
            {summaryText(result.summary)}
          </p>
          {result.truncated === true && <p className={NOTE}>{TRUNCATED_NOTICE}</p>}
          {result.occurrences.length === 0 ? (
            <p className={NOTE}>{EMPTY_RESULT}</p>
          ) : (
            // Server (source) order; never re-sorted, deduplicated or merged here.
            <ul className="space-y-0.5" aria-label="Name occurrence list">
              {result.occurrences.map((occurrence, index) => (
                <li key={`${index}:${occurrence.line}:${occurrence.column}`} data-testid="occurrence">
                  {onSelectLine && target !== undefined ? (
                    <button
                      type="button"
                      onClick={() => onSelectLine(target, occurrence.line)}
                      className="text-left font-mono text-slate-300 hover:underline"
                    >
                      {occurrenceLabel(occurrence)}
                    </button>
                  ) : (
                    <span className="font-mono text-slate-300">{occurrenceLabel(occurrence)}</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}
