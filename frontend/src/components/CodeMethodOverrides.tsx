import { useCodeMethodOverrides } from '../hooks/useCodeMethodOverrides'
import { getErrorMessage } from '../services/client'
import type {
  DefinitionLocation,
  MemberStatus,
  MethodOverridesStatus,
  MethodOverridesSummary,
  OverriddenEntry,
  OverridingEntry,
} from '../types/api'
import { KIND_MESSAGES } from './CodeTypeHierarchy'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const NO_BASES = "This method's class does not directly inherit from another class."
export const NO_OVERRIDES = 'No direct overrides found.'
export const OVERRIDES_UNSUPPORTED = "Override analysis is not available for this method's class."
export const OVERRIDDEN_TRUNCATED = 'Overridden-method results are truncated.'
export const OVERRIDING_TRUNCATED = 'Override results are truncated.'
export const SEVERAL_DEFINITIONS = 'Defined more than once in this class'
export const UNKNOWN_STATUS = 'Method overrides are unavailable.'
export const STATUS_MESSAGES: Record<Exclude<MethodOverridesStatus, 'scanned'>, string> = {
  not_a_definition: 'Method overrides are unavailable: no method starts on this line.',
  not_a_method: 'Method overrides are only available for methods.',
  syntax_error: 'Method overrides are unavailable: the target file could not be parsed as Python.',
  too_large: 'Method overrides are unavailable: the target file is too large to analyze.',
  binary: 'Method overrides are unavailable: the target file is not text.',
  unsupported_language: 'Method overrides are only available for Python files.',
}
export const MEMBER_MESSAGES: Record<MemberStatus, string> = {
  defined: 'Defined in this class',
  not_defined: 'Not defined in this class',
  not_a_method: 'Bound in this class, but not as a method',
  unsupported: 'Static analysis could not determine whether this class defines the method.',
  unavailable: 'The base class could not be resolved to a repository class.',
}

/** The server's counts, never derived from the rows shown (they are counted before the caps). */
export function overridesSummary(s: MethodOverridesSummary): string {
  return (
    `Bases: ${s.bases} · ${s.overridden_defined} defined · ${s.overridden_unavailable} unavailable · ` +
    `${s.overridden_unsupported} unsupported. ` +
    `Subclasses: ${s.subclasses} · ${s.overriding} overriding · ${s.inheriting} inheriting · ` +
    `${s.overriding_unsupported} unsupported.`
  )
}

function identity(definition: DefinitionLocation): string {
  return `${definition.kind} ${definition.name} (${definition.path}, line ${definition.line})`
}

function definitionText(definition: DefinitionLocation): string {
  return `${definition.name} ${definition.kind} ${definition.path} lines ${definition.line}–${definition.end_line}`
}

/** The exact identity of a Step 53 target: its path and its start line. */
function sameTarget(requested: DefinitionLocation | undefined, definition: DefinitionLocation) {
  return (
    requested !== undefined &&
    requested.path === definition.path &&
    requested.line === definition.line
  )
}

type Navigate = (path: string, line: number) => void

/** A button opening `path` at `line` when navigation is available; plain text otherwise. */
function Jump({
  label,
  path,
  line,
  text,
  className,
  onSelectLine,
}: {
  label: string
  path: string
  line: number
  text: string
  className: string
  onSelectLine?: Navigate
}) {
  return onSelectLine ? (
    <button
      type="button"
      aria-label={label}
      onClick={() => onSelectLine(path, line)}
      className={`text-left hover:underline ${className}`}
    >
      {text}
    </button>
  ) : (
    <span className={className}>{text}</span>
  )
}

/** A related class's member status and the server's implementations, as returned. */
function Member({
  status,
  reason,
  several,
  implementations,
  onSelectLine,
}: {
  status: MemberStatus
  /** why an unavailable base has no class to inspect (its Step 51 kind) */
  reason?: string
  /** the server's flag: counted before the implementation list is cut */
  several: boolean
  implementations: DefinitionLocation[]
  onSelectLine?: Navigate
}) {
  return (
    <>
      <p className="text-slate-400" data-testid="member-status">
        {MEMBER_MESSAGES[status] ?? MEMBER_MESSAGES.unsupported}
      </p>
      {reason && (
        <p className="text-slate-500" data-testid="base-kind">
          {reason}
        </p>
      )}
      {several && (
        <p className="text-slate-500" data-testid="several">
          {SEVERAL_DEFINITIONS}
        </p>
      )}
      {/* Every server implementation, in server order with duplicates; never one chosen. */}
      {implementations.length > 0 && (
        <ul className="ml-4 space-y-0.5">
          {implementations.map((implementation, index) => (
            <li key={`${index}:${implementation.path}:${implementation.line}`} data-testid="implementation">
              <Jump
                label={`Open implementation ${implementation.kind} ${implementation.name} in ${implementation.path} at line ${implementation.line}`}
                path={implementation.path}
                line={implementation.line}
                text={definitionText(implementation)}
                className="text-slate-300"
                onSelectLine={onSelectLine}
              />
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

/** One direct base's class candidate: the base site (in the target's file), the class, its status. */
function Overridden({
  entry,
  targetPath,
  onSelectLine,
}: {
  entry: OverriddenEntry
  /** base sites have no path of their own: they are positions in the target's file */
  targetPath: string
  onSelectLine?: Navigate
}) {
  const { site } = entry
  const cls = entry.class // the frozen JSON key is "class"
  const reason =
    entry.status === 'unavailable'
      ? KIND_MESSAGES[entry.base_kind as keyof typeof KIND_MESSAGES]
      : undefined
  return (
    <li data-testid="overridden" className="space-y-0.5">
      {site ? (
        <Jump
          label={`Jump to ${targetPath} at line ${site.line} (base ${site.name})`}
          path={targetPath}
          line={site.line}
          text={`${site.name} line ${site.line}, column ${site.column}`}
          className="text-slate-200"
          onSelectLine={onSelectLine}
        />
      ) : (
        <span className="text-slate-200">Dynamic base expression</span>
      )}
      {cls && (
        <div data-testid="base-class">
          <Jump
            label={`Open base class ${cls.name} in ${cls.path} at line ${cls.line}${entry.ambiguous ? ', ambiguous' : ''}`}
            path={cls.path}
            line={cls.line}
            text={`${definitionText(cls)}${entry.ambiguous ? ' · ambiguous' : ''}`}
            className="text-slate-300"
            onSelectLine={onSelectLine}
          />
        </div>
      )}
      <Member
        status={entry.status}
        reason={reason}
        several={entry.implementations_ambiguous}
        implementations={entry.implementations}
        onSelectLine={onSelectLine}
      />
    </li>
  )
}

/** One direct subclass entry binding the method's name; its site is a position in subclass.path. */
function Overriding({ entry, onSelectLine }: { entry: OverridingEntry; onSelectLine?: Navigate }) {
  const { subclass, site } = entry
  return (
    <li data-testid="overriding" className="space-y-0.5">
      <Jump
        label={`Open subclass ${subclass.kind} ${subclass.name} in ${subclass.path} at line ${subclass.line}`}
        path={subclass.path}
        line={subclass.line}
        text={definitionText(subclass)}
        className="text-slate-200"
        onSelectLine={onSelectLine}
      />
      <div data-testid="overriding-site">
        <Jump
          label={`Jump to ${subclass.path} at line ${site.line} (inherits ${site.name}${site.ambiguous ? ', ambiguous' : ''})`}
          path={subclass.path}
          line={site.line}
          text={`Inherits at line ${site.line}, column ${site.column}${site.ambiguous ? ' · ambiguous' : ''}`}
          className="text-slate-400"
          onSelectLine={onSelectLine}
        />
      </div>
      <Member
        status={entry.status}
        several={entry.implementations_ambiguous}
        implementations={entry.implementations}
        onSelectLine={onSelectLine}
      />
    </li>
  )
}

interface CodeMethodOverridesProps {
  projectId: string
  repositoryId: string
  /** The server's definitions of a resolved or ambiguous lookup; null when there are none. */
  definitions: DefinitionLocation[] | null
  /** When given, every definition and site becomes a button opening its file at its line. */
  onSelectLine?: Navigate
}

/**
 * On-demand direct method overrides for the method definitions of one go-to-definition
 * lookup. Nothing is requested until a method's button is clicked, and only one request runs
 * at a time. CodeDefinition remounts this panel for every new lookup (and FileViewer for every
 * file), so earlier results, errors and pending requests never outlive their lookup. The
 * server's entries are shown as returned: never re-resolved, sorted, deduplicated, regrouped,
 * filtered or recounted.
 */
export function CodeMethodOverrides({
  projectId,
  repositoryId,
  definitions,
  onSelectLine,
}: CodeMethodOverridesProps) {
  const overrides = useCodeMethodOverrides(projectId, repositoryId)
  // Server order. Only methods have overrides; nothing else is ever requested.
  const targets = (definitions ?? [])
    .map((definition, index) => ({ definition, index }))
    .filter(({ definition }) => definition.kind === 'method')
  if (targets.length === 0) {
    return null
  }

  const result = overrides.isSuccess ? overrides.data : undefined
  // The server's target; the exact requested definition when the server reports none.
  const subject = result ? (result.target ?? overrides.variables) : undefined
  const scanned = result !== undefined && result.status === 'scanned'
  const unavailable =
    result && !scanned
      ? (STATUS_MESSAGES[result.status as keyof typeof STATUS_MESSAGES] ?? UNKNOWN_STATUS)
      : null
  return (
    <section className="space-y-2 border-t border-slate-800 pt-2" aria-label="Method overrides">
      <div className="flex flex-col items-start gap-1">
        {targets.map(({ definition, index }) => {
          const pending = overrides.isPending && sameTarget(overrides.variables, definition)
          return (
            <button
              key={`${index}:${definition.path}:${definition.line}`}
              type="button"
              disabled={overrides.isPending}
              onClick={() => overrides.mutate(definition)}
              className="rounded-lg border border-slate-700 px-2 py-1 text-left text-xs text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {pending
                ? `Loading method overrides for ${identity(definition)}…`
                : `Method overrides for ${identity(definition)}`}
            </button>
          )
        })}
      </div>
      {overrides.isError && (
        <ErrorMessage
          message={getErrorMessage(overrides.error, 'Could not load the method overrides')}
        />
      )}
      {subject && (
        <p className={NOTE} data-testid="method-overrides-target">
          {`Method overrides of ${identity(subject)}${result?.owner ? ` in class ${result.owner.name}` : ''}`}
        </p>
      )}
      {unavailable && <p className={NOTE}>{unavailable}</p>}
      {result && scanned && result.target && (
        <div className="space-y-2 text-xs">
          <p className="text-slate-500" data-testid="method-overrides-summary">
            {overridesSummary(result.summary)}
          </p>
          <h4 className="font-semibold text-slate-300">Overridden in direct base classes</h4>
          {result.overridden.length === 0 ? (
            <p className={NOTE}>{NO_BASES}</p>
          ) : (
            <ul className="space-y-1" aria-label="Overridden in direct base classes">
              {/* Server order (bases, then candidates); never re-sorted, merged or filtered. */}
              {result.overridden.map((entry, index) => (
                <Overridden
                  key={`${index}:${entry.site?.line ?? ''}:${entry.site?.column ?? ''}`}
                  entry={entry}
                  targetPath={result.target?.path ?? ''}
                  onSelectLine={onSelectLine}
                />
              ))}
            </ul>
          )}
          {result.overridden_truncated === true && <p className={NOTE}>{OVERRIDDEN_TRUNCATED}</p>}
          <h4 className="font-semibold text-slate-300">Direct overrides</h4>
          {result.overriding_status === 'unsupported_target' || result.overriding === null ? (
            <p className={NOTE}>{OVERRIDES_UNSUPPORTED}</p>
          ) : result.overriding.length === 0 ? (
            <p className={NOTE}>{NO_OVERRIDES}</p>
          ) : (
            <ul className="space-y-1" aria-label="Direct overrides">
              {result.overriding.map((entry, index) => (
                <Overriding
                  key={`${index}:${entry.subclass.path}:${entry.site.line}:${entry.site.column}`}
                  entry={entry}
                  onSelectLine={onSelectLine}
                />
              ))}
            </ul>
          )}
          {result.overriding_truncated === true && <p className={NOTE}>{OVERRIDING_TRUNCATED}</p>}
        </div>
      )}
    </section>
  )
}
