import { useCodeCallHierarchy } from '../hooks/useCodeCallHierarchy'
import { getErrorMessage } from '../services/client'
import type {
  CallHierarchyStatus,
  CallHierarchySummary,
  CallSite,
  DefinitionLocation,
} from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const NO_CALLERS = 'No callers found.'
export const NO_CALLEES = 'No callees found.'
export const CALLERS_UNSUPPORTED = 'Caller analysis is not available for this target.'
export const CALLERS_TRUNCATED = 'Caller results are truncated.'
export const CALLEES_TRUNCATED = 'Callee results are truncated.'
export const UNKNOWN_STATUS = 'Call hierarchy is unavailable.'
export const STATUS_MESSAGES: Record<Exclude<CallHierarchyStatus, 'scanned'>, string> = {
  not_a_definition: 'Call hierarchy is unavailable: no class or function starts on this line.',
  syntax_error: 'Call hierarchy is unavailable: the target file could not be parsed as Python.',
  too_large: 'Call hierarchy is unavailable: the target file is too large to analyze.',
  binary: 'Call hierarchy is unavailable: the target file is not text.',
  unsupported_language: 'Call hierarchy is only available for Python files.',
}

/** The server's counts, never derived from the rows shown (they are counted before the caps). */
export function callerSummary(s: CallHierarchySummary): string {
  return `Callers: ${s.caller_calls} calls · ${s.caller_ambiguous} ambiguous · ${s.caller_unsupported} unsupported.`
}

export function calleeSummary(s: CallHierarchySummary): string {
  return (
    `Callees: ${s.callee_calls} calls · ${s.callee_ambiguous} ambiguous · ` +
    `${s.callee_unsupported} unsupported · ${s.callee_builtin} builtin · ` +
    `${s.callee_unresolved} unresolved · ${s.callee_dynamic} dynamic.`
  )
}

function identity(definition: DefinitionLocation): string {
  return `${definition.kind} ${definition.name} (${definition.path}, line ${definition.line})`
}

/** The exact identity of a Step 49 target: its path and its start line. */
function sameTarget(requested: DefinitionLocation | undefined, definition: DefinitionLocation) {
  return (
    requested !== undefined &&
    requested.path === definition.path &&
    requested.line === definition.line
  )
}

type Navigate = (path: string, line: number) => void

/** One group: its definition and its calls, only named fields, always as plain text. */
function Group({
  role,
  definition,
  total,
  calls,
  callsPath,
  onSelectLine,
}: {
  role: 'caller' | 'callee'
  definition: DefinitionLocation
  total: number
  calls: CallSite[]
  /** the file the calls are in: caller.path for callers, the target's path for callees */
  callsPath: string
  onSelectLine?: Navigate
}) {
  const header = `${definition.name} ${definition.kind} ${definition.path} lines ${definition.line}–${definition.end_line} · ${total} calls`
  return (
    <li data-testid={role} className="space-y-0.5">
      {onSelectLine ? (
        <button
          type="button"
          aria-label={`Open ${role} ${definition.kind} ${definition.name} in ${definition.path} at line ${definition.line}`}
          onClick={() => onSelectLine(definition.path, definition.line)}
          className="text-left text-slate-200 hover:underline"
        >
          {header}
        </button>
      ) : (
        <span className="text-slate-200">{header}</span>
      )}
      {/* Server order; never re-sorted, deduplicated or filtered here. */}
      <ul className="ml-4 space-y-0.5">
        {calls.map((call, index) => {
          const text = `${call.name} line ${call.line}, column ${call.column}${call.ambiguous ? ' · ambiguous' : ''}`
          return (
            <li key={`${index}:${call.line}:${call.column}`} data-testid={`${role}-call`}>
              {onSelectLine ? (
                <button
                  type="button"
                  aria-label={`Jump to ${callsPath} at line ${call.line} (${call.name}${call.ambiguous ? ', ambiguous' : ''})`}
                  onClick={() => onSelectLine(callsPath, call.line)}
                  className="text-left text-slate-400 hover:underline"
                >
                  {text}
                </button>
              ) : (
                <span className="text-slate-400">{text}</span>
              )}
            </li>
          )
        })}
      </ul>
    </li>
  )
}

interface CodeCallHierarchyProps {
  projectId: string
  repositoryId: string
  /** The server's definitions of a resolved or ambiguous lookup; null when there are none. */
  definitions: DefinitionLocation[] | null
  /** When given, every group and call becomes a button opening its file at its line. */
  onSelectLine?: Navigate
}

/**
 * On-demand call hierarchy for the definitions of one go-to-definition lookup. Nothing is
 * requested until a definition's button is clicked, and only one request runs at a time.
 * CodeDefinition remounts this panel for every new lookup (and FileViewer for every file),
 * so earlier results, errors and pending requests never outlive their lookup. The server's
 * groups and calls are shown as returned: never re-resolved, sorted, deduplicated or filtered.
 */
export function CodeCallHierarchy({
  projectId,
  repositoryId,
  definitions,
  onSelectLine,
}: CodeCallHierarchyProps) {
  const hierarchy = useCodeCallHierarchy(projectId, repositoryId)
  // Server order. Modules are never Step 49 targets: their line 1 may start another statement.
  const targets = (definitions ?? [])
    .map((definition, index) => ({ definition, index }))
    .filter(({ definition }) => definition.kind !== 'module')
  if (targets.length === 0) {
    return null
  }

  const result = hierarchy.isSuccess ? hierarchy.data : undefined
  // The server's target; the exact requested definition when the server reports none.
  const subject = result ? (result.target ?? hierarchy.variables) : undefined
  const scanned = result !== undefined && result.status === 'scanned'
  const unavailable =
    result && !scanned
      ? (STATUS_MESSAGES[result.status as keyof typeof STATUS_MESSAGES] ?? UNKNOWN_STATUS)
      : null
  return (
    <section className="space-y-2 border-t border-slate-800 pt-2" aria-label="Call hierarchy">
      <div className="flex flex-col items-start gap-1">
        {targets.map(({ definition, index }) => {
          const pending = hierarchy.isPending && sameTarget(hierarchy.variables, definition)
          return (
            <button
              key={`${index}:${definition.path}:${definition.line}`}
              type="button"
              disabled={hierarchy.isPending}
              onClick={() => hierarchy.mutate(definition)}
              className="rounded-lg border border-slate-700 px-2 py-1 text-left text-xs text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {pending
                ? `Loading call hierarchy for ${identity(definition)}…`
                : `Call hierarchy for ${identity(definition)}`}
            </button>
          )
        })}
      </div>
      {hierarchy.isError && (
        <ErrorMessage
          message={getErrorMessage(hierarchy.error, 'Could not load the call hierarchy')}
        />
      )}
      {subject && (
        <p className={NOTE} data-testid="call-hierarchy-target">
          {`Calls of ${identity(subject)}`}
        </p>
      )}
      {unavailable && <p className={NOTE}>{unavailable}</p>}
      {result && scanned && result.target && (
        <div className="space-y-2 text-xs">
          <p className="text-slate-500" data-testid="call-hierarchy-summary">
            {callerSummary(result.summary)} {calleeSummary(result.summary)}
          </p>
          <h4 className="font-semibold text-slate-300">Callers</h4>
          {result.callers_status === 'unsupported_target' || result.callers === null ? (
            <p className={NOTE}>{CALLERS_UNSUPPORTED}</p>
          ) : result.callers.length === 0 ? (
            <p className={NOTE}>{NO_CALLERS}</p>
          ) : (
            <ul className="space-y-1" aria-label="Callers">
              {result.callers.map((group, index) => (
                <Group
                  key={`${index}:${group.caller.path}:${group.caller.line}`}
                  role="caller"
                  definition={group.caller}
                  total={group.total_calls}
                  calls={group.calls}
                  callsPath={group.caller.path}
                  onSelectLine={onSelectLine}
                />
              ))}
            </ul>
          )}
          {result.callers_truncated === true && <p className={NOTE}>{CALLERS_TRUNCATED}</p>}
          <h4 className="font-semibold text-slate-300">Callees</h4>
          {result.callees.length === 0 ? (
            <p className={NOTE}>{NO_CALLEES}</p>
          ) : (
            <ul className="space-y-1" aria-label="Callees">
              {result.callees.map((group, index) => (
                <Group
                  key={`${index}:${group.callee.path}:${group.callee.line}`}
                  role="callee"
                  definition={group.callee}
                  total={group.total_calls}
                  calls={group.calls}
                  callsPath={result.target?.path ?? ''}
                  onSelectLine={onSelectLine}
                />
              ))}
            </ul>
          )}
          {result.callees_truncated && <p className={NOTE}>{CALLEES_TRUNCATED}</p>}
        </div>
      )}
    </section>
  )
}
