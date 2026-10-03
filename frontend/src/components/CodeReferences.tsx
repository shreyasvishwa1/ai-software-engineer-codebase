import { useCodeReferences } from '../hooks/useCodeReferences'
import { getErrorMessage } from '../services/client'
import type {
  DefinitionLocation,
  ReferenceLocation,
  ReferencesStatus,
  ReferencesSummary,
} from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const NO_REFERENCES = 'No references found.'
export const TRUNCATED_NOTICE =
  'The reference list is incomplete: a scan or result limit was reached (at most 500 references are shown).'
export const UNKNOWN_STATUS = 'Find references is unavailable.'
export const STATUS_MESSAGES: Record<Exclude<ReferencesStatus, 'scanned'>, string> = {
  not_a_definition: 'Find references is unavailable: no class or function starts on this line.',
  unsupported_target: 'Find references does not support methods or nested classes.',
  syntax_error: 'Find references is unavailable: the target file could not be parsed as Python.',
  too_large: 'Find references is unavailable: the target file is too large to analyze.',
  binary: 'Find references is unavailable: the target file is not text.',
  unsupported_language: 'Find references is only available for Python files.',
}

/** The server's counts, never derived from the rows shown (they are counted before the cap). */
export function summaryText(summary: ReferencesSummary): string {
  return `${summary.references} references · ${summary.ambiguous} ambiguous · ${summary.files_scanned} files scanned.`
}

function identity(definition: DefinitionLocation): string {
  return `${definition.kind} ${definition.name} (${definition.path}, line ${definition.line})`
}

/** The exact identity of a Step 47 target: its path and its start line. */
function sameTarget(requested: DefinitionLocation | undefined, definition: DefinitionLocation) {
  return (
    requested !== undefined &&
    requested.path === definition.path &&
    requested.line === definition.line
  )
}

interface CodeReferencesProps {
  projectId: string
  repositoryId: string
  /** The server's definitions of a resolved or ambiguous lookup; null when there are none. */
  definitions: DefinitionLocation[] | null
  /** When given, every reference becomes a button that opens its file at its line. */
  onSelectLine?: (path: string, line: number) => void
}

/** One reference: only its named fields, always as plain text. */
function ReferenceRow({
  reference,
  onSelectLine,
}: {
  reference: ReferenceLocation
  onSelectLine?: (path: string, line: number) => void
}) {
  const text = (
    <>
      <span className="break-all font-mono text-slate-300">{reference.path}</span>{' '}
      <span className="text-slate-500">{`line ${reference.line}, column ${reference.column}`}</span>{' '}
      <span className="font-mono text-slate-200">{reference.name}</span>{' '}
      <span className="text-slate-500">{reference.context}</span>
      {reference.ambiguous === true && (
        <>
          {' '}
          <span className="text-amber-300">ambiguous</span>
        </>
      )}
    </>
  )
  const label = `Go to ${reference.path} at line ${reference.line} (${reference.context} ${reference.name}${
    reference.ambiguous === true ? ', ambiguous' : ''
  })`
  return (
    <li data-testid="reference">
      {onSelectLine ? (
        // The line only: the column is shown but never navigated to.
        <button
          type="button"
          aria-label={label}
          onClick={() => onSelectLine(reference.path, reference.line)}
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
 * On-demand find-references for the definitions of one go-to-definition lookup. Nothing is
 * requested until a definition's button is clicked, and only one request runs at a time.
 * CodeDefinition remounts this panel for every new lookup (and FileViewer for every file),
 * so earlier results, errors and pending requests never outlive their lookup. The server's
 * references are shown as returned: never re-resolved, sorted, deduplicated or filtered.
 */
export function CodeReferences({
  projectId,
  repositoryId,
  definitions,
  onSelectLine,
}: CodeReferencesProps) {
  const references = useCodeReferences(projectId, repositoryId)
  // Server order. Modules are never Step 47 targets: their line 1 may start another statement.
  const targets = (definitions ?? [])
    .map((definition, index) => ({ definition, index }))
    .filter(({ definition }) => definition.kind !== 'module')
  if (targets.length === 0) {
    return null
  }

  const result = references.isSuccess ? references.data : undefined
  // The server's target; the exact requested definition when the server reports none.
  const subject = result ? (result.target ?? references.variables) : undefined
  const unavailable =
    result && result.status !== 'scanned'
      ? (STATUS_MESSAGES[result.status as keyof typeof STATUS_MESSAGES] ?? UNKNOWN_STATUS)
      : null
  return (
    <section className="space-y-2 border-t border-slate-800 pt-2" aria-label="Find references">
      <div className="flex flex-col items-start gap-1">
        {targets.map(({ definition, index }) => {
          const pending = references.isPending && sameTarget(references.variables, definition)
          return (
            <button
              key={`${index}:${definition.path}:${definition.line}`}
              type="button"
              disabled={references.isPending}
              onClick={() => references.mutate(definition)}
              className="rounded-lg border border-slate-700 px-2 py-1 text-left text-xs text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {pending
                ? `Finding references to ${identity(definition)}…`
                : `Find references to ${identity(definition)}`}
            </button>
          )
        })}
      </div>
      {references.isError && (
        <ErrorMessage message={getErrorMessage(references.error, 'Could not find the references')} />
      )}
      {subject && (
        <p className={NOTE} data-testid="references-target">
          {`Results for ${identity(subject)}`}
        </p>
      )}
      {unavailable && <p className={NOTE}>{unavailable}</p>}
      {result && result.truncated === true && <p className={NOTE}>{TRUNCATED_NOTICE}</p>}
      {result && result.status === 'scanned' && (
        <>
          {result.references.length === 0 ? (
            <p className={NOTE}>{NO_REFERENCES}</p>
          ) : (
            // Server order (path, line, column); never re-sorted, deduplicated or filtered here.
            <ul className="space-y-0.5 text-xs" aria-label="References">
              {result.references.map((reference, index) => (
                <ReferenceRow
                  key={`${index}:${reference.path}:${reference.line}:${reference.column}`}
                  reference={reference}
                  onSelectLine={onSelectLine}
                />
              ))}
            </ul>
          )}
          <p className="text-xs text-slate-500" data-testid="references-summary">
            {summaryText(result.summary)}
          </p>
        </>
      )}
    </section>
  )
}
