import { useState } from 'react'

import { useCodeDefinition } from '../hooks/useCodeDefinition'
import { getErrorMessage } from '../services/client'
import type { CodePosition } from '../services/codeDefinition'
import type { DefinitionLocation, DefinitionStatus } from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const NO_POSITION = 'Place the cursor on an identifier in the code first.'
export const TRUNCATED_NOTICE =
  'The definition search is incomplete: a file, import or result limit was reached.'
export const UNKNOWN_STATUS = 'Go to definition is unavailable.'
export const STATUS_MESSAGES: Record<Exclude<DefinitionStatus, 'resolved' | 'ambiguous'>, string> = {
  not_found: 'Definition not found.',
  unsupported: 'Go to definition is not supported for this reference.',
  no_identifier: 'No identifier at this position.',
  syntax_error: 'Go to definition is unavailable: this file could not be parsed as Python.',
  too_large: 'Go to definition is unavailable: this file is too large to analyze (limit 256 KiB).',
  binary: 'Go to definition is unavailable: this file is not text.',
  unsupported_language: 'Go to definition is only available for Python files.',
}

/**
 * The 1-based line and 1-based code-point column of a UTF-16 offset into `content`. Line
 * breaks are Python's: "\r\n", "\r" and "\n". Columns count Unicode code points (a tab is
 * one), never UTF-16 units; nothing is normalized.
 */
export function positionAt(content: string, utf16Offset: number): CodePosition {
  const offset = Math.min(Math.max(0, utf16Offset), content.length)
  let line = 1
  let lineStart = 0
  for (let i = 0; i < offset; i++) {
    const char = content[i]
    if (char === '\n' || (char === '\r' && content[i + 1] !== '\n')) {
      line += 1
      lineStart = i + 1
    }
  }
  return { line, column: Array.from(content.slice(lineStart, offset)).length + 1 }
}

/**
 * UTF-16 offset, within `container`'s text, of the start of the browser's current selection
 * (or caret); null when there is none or it does not start inside `container`.
 */
export function selectionOffset(container: HTMLElement): number | null {
  const selection = container.ownerDocument.getSelection()
  if (!selection || selection.rangeCount === 0) {
    return null
  }
  const range = selection.getRangeAt(0)
  if (!container.contains(range.startContainer)) {
    return null
  }
  const before = container.ownerDocument.createRange()
  before.setStart(container, 0)
  before.setEnd(range.startContainer, range.startOffset)
  return before.toString().length
}

interface CodeDefinitionProps {
  projectId: string
  repositoryId: string
  /** The open file's server-provided path. */
  path: string
  language: string | null
  /** The open file's text, exactly as displayed. */
  content: string
  /** UTF-16 offset of the current selection in the displayed text, read on click only. */
  getOffset: () => number | null
  /** When given, every definition becomes a button that opens its file at its line. */
  onSelectLine?: (path: string, line: number) => void
}

/** One definition: only its named fields, always as plain text. */
function DefinitionRow({
  definition,
  onSelectLine,
}: {
  definition: DefinitionLocation
  onSelectLine?: (path: string, line: number) => void
}) {
  const text = (
    <>
      <span className="font-mono text-slate-200">{definition.name}</span>{' '}
      <span className="text-slate-500">{definition.kind}</span>{' '}
      <span className="break-all font-mono text-slate-300">{definition.path}</span>{' '}
      <span className="text-slate-500">{`lines ${definition.line}–${definition.end_line}`}</span>
    </>
  )
  return (
    <li data-testid="definition">
      {onSelectLine ? (
        <button
          type="button"
          aria-label={`Open ${definition.path} at line ${definition.line} (${definition.kind} ${definition.name})`}
          onClick={() => onSelectLine(definition.path, definition.line)}
          className="text-left hover:underline"
        >
          {text}
        </button>
      ) : (
        text
      )}
    </li>
  )
}

/**
 * On-demand go-to-definition for the displayed Python file. Nothing is requested until the
 * button is clicked; the position comes from the browser's selection at that moment. The
 * component is keyed by path, so another file always starts fresh. The server's
 * definitions are shown as returned: never re-resolved, sorted, filtered or chosen from.
 */
export function CodeDefinition({
  projectId,
  repositoryId,
  path,
  language,
  content,
  getOffset,
  onSelectLine,
}: CodeDefinitionProps) {
  const definition = useCodeDefinition(projectId, repositoryId)
  const [noPosition, setNoPosition] = useState(false)
  if (language !== 'Python') {
    return null
  }

  const goToDefinition = () => {
    const offset = getOffset()
    if (offset === null) {
      setNoPosition(true)
      return
    }
    setNoPosition(false)
    definition.mutate({ path, position: positionAt(content, offset) })
  }

  const result = definition.isSuccess ? definition.data : undefined
  const found = result && (result.status === 'resolved' || result.status === 'ambiguous')
  const unavailable =
    result && !found
      ? (STATUS_MESSAGES[result.status as keyof typeof STATUS_MESSAGES] ?? UNKNOWN_STATUS)
      : null
  return (
    <section className="space-y-2 rounded-lg border border-slate-800 p-3" aria-label="Go to definition">
      <button
        type="button"
        disabled={definition.isPending}
        // Keep the code selection: the click must not move focus or clear it.
        onMouseDown={(event) => event.preventDefault()}
        onClick={goToDefinition}
        className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {definition.isPending ? 'Finding definition…' : 'Go to definition'}
      </button>
      {noPosition && <p className={NOTE}>{NO_POSITION}</p>}
      {definition.isError && (
        <ErrorMessage message={getErrorMessage(definition.error, 'Could not find the definition')} />
      )}
      {unavailable && <p className={NOTE}>{unavailable}</p>}
      {result && result.truncated === true && <p className={NOTE}>{TRUNCATED_NOTICE}</p>}
      {result && found && (
        <>
          {result.reference && (
            <p className={NOTE}>
              {result.status === 'ambiguous'
                ? `${result.definitions.length} possible definitions of `
                : 'Definition of '}
              <span className="font-mono text-slate-200">{result.reference.name}</span>
            </p>
          )}
          {/* Every server definition, in server order; never one chosen automatically. */}
          <ul className="space-y-0.5 text-xs" aria-label="Definitions">
            {result.definitions.map((location, index) => (
              <DefinitionRow
                key={`${index}:${location.path}:${location.line}`}
                definition={location}
                onSelectLine={onSelectLine}
              />
            ))}
          </ul>
        </>
      )}
    </section>
  )
}
