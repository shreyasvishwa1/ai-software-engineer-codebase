import { useCodeTypeHierarchy } from '../hooks/useCodeTypeHierarchy'
import { getErrorMessage } from '../services/client'
import type {
  BaseEntry,
  BaseKind,
  DefinitionLocation,
  SubclassEntry,
  TypeHierarchyStatus,
  TypeHierarchySummary,
} from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const NO_BASES = 'This class does not directly inherit from another class.'
export const NO_SUBCLASSES = 'No direct subclasses found.'
export const SUBCLASSES_UNSUPPORTED = 'Subclass analysis is not available for this class.'
export const BASES_TRUNCATED = 'Base class results are truncated.'
export const SUBCLASSES_TRUNCATED = 'Subclass results are truncated.'
export const UNKNOWN_STATUS = 'Type hierarchy is unavailable.'
export const STATUS_MESSAGES: Record<Exclude<TypeHierarchyStatus, 'scanned'>, string> = {
  not_a_definition: 'Type hierarchy is unavailable: no class starts on this line.',
  not_a_class: 'Type hierarchy is only available for classes.',
  syntax_error: 'Type hierarchy is unavailable: the target file could not be parsed as Python.',
  too_large: 'Type hierarchy is unavailable: the target file is too large to analyze.',
  binary: 'Type hierarchy is unavailable: the target file is not text.',
  unsupported_language: 'Type hierarchy is only available for Python files.',
}
export const KIND_MESSAGES: Record<Exclude<BaseKind, 'ambiguous'>, string> = {
  resolved: 'Resolved class',
  not_a_class: 'Not a class',
  builtin: 'Built-in class',
  unsupported: 'Static resolution is not supported for this base.',
  unresolved: 'Base class could not be resolved.',
  dynamic: 'Dynamic base: the base class could not be resolved statically.',
}

/** The server's counts, never derived from the rows shown (they are counted before the caps). */
export function hierarchySummary(s: TypeHierarchySummary): string {
  return (
    `Bases: ${s.bases} · ${s.bases_resolved} resolved · ${s.bases_ambiguous} ambiguous · ` +
    `${s.bases_unresolved} unresolved · ${s.bases_dynamic} dynamic. ` +
    `Subclasses: ${s.subclasses} · ${s.subclasses_ambiguous} ambiguous.`
  )
}

function identity(definition: DefinitionLocation): string {
  return `${definition.kind} ${definition.name} (${definition.path}, line ${definition.line})`
}

/** The exact identity of a Step 51 target: its path and its start line. */
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

function definitionText(definition: DefinitionLocation): string {
  return `${definition.name} ${definition.kind} ${definition.path} lines ${definition.line}–${definition.end_line}`
}

/** One base: its site in the target's file, its kind and Step 45's definitions, as returned. */
function Base({
  base,
  targetPath,
  onSelectLine,
}: {
  base: BaseEntry
  /** base sites have no path of their own: they are positions in the target's file */
  targetPath: string
  onSelectLine?: Navigate
}) {
  const kind =
    base.kind === 'ambiguous'
      ? `Ambiguous: ${base.definitions.length} candidates`
      : (KIND_MESSAGES[base.kind] ?? KIND_MESSAGES.unresolved)
  return (
    <li data-testid="base" className="space-y-0.5">
      {base.site ? (
        <Jump
          label={`Jump to ${targetPath} at line ${base.site.line} (base ${base.site.name})`}
          path={targetPath}
          line={base.site.line}
          text={`${base.site.name} line ${base.site.line}, column ${base.site.column}`}
          className="text-slate-200"
          onSelectLine={onSelectLine}
        />
      ) : (
        <span className="text-slate-200">Dynamic base expression</span>
      )}
      <p className="text-slate-400" data-testid="base-kind">
        {kind}
      </p>
      {/* Every server definition, in server order with duplicates; never one chosen. */}
      {base.definitions.length > 0 && (
        <ul className="ml-4 space-y-0.5">
          {base.definitions.map((definition, index) => (
            <li key={`${index}:${definition.path}:${definition.line}`} data-testid="base-definition">
              <Jump
                label={`Open base ${definition.kind} ${definition.name} in ${definition.path} at line ${definition.line}`}
                path={definition.path}
                line={definition.line}
                text={definitionText(definition)}
                className="text-slate-300"
                onSelectLine={onSelectLine}
              />
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

/** One direct subclass and where it names the target (a position in subclass.path). */
function Subclass({ entry, onSelectLine }: { entry: SubclassEntry; onSelectLine?: Navigate }) {
  const { subclass, site } = entry
  return (
    <li data-testid="subclass" className="space-y-0.5">
      <Jump
        label={`Open subclass ${subclass.kind} ${subclass.name} in ${subclass.path} at line ${subclass.line}`}
        path={subclass.path}
        line={subclass.line}
        text={definitionText(subclass)}
        className="text-slate-200"
        onSelectLine={onSelectLine}
      />
      <div className="ml-4" data-testid="subclass-site">
        <Jump
          label={`Jump to ${subclass.path} at line ${site.line} (inherits ${site.name}${site.ambiguous ? ', ambiguous' : ''})`}
          path={subclass.path}
          line={site.line}
          text={`Inherits at line ${site.line}, column ${site.column}${site.ambiguous ? ' · ambiguous' : ''}`}
          className="text-slate-400"
          onSelectLine={onSelectLine}
        />
      </div>
    </li>
  )
}

interface CodeTypeHierarchyProps {
  projectId: string
  repositoryId: string
  /** The server's definitions of a resolved or ambiguous lookup; null when there are none. */
  definitions: DefinitionLocation[] | null
  /** When given, every definition and site becomes a button opening its file at its line. */
  onSelectLine?: Navigate
}

/**
 * On-demand type hierarchy (direct bases and direct subclasses) for the class definitions of
 * one go-to-definition lookup. Nothing is requested until a class's button is clicked, and
 * only one request runs at a time. CodeDefinition remounts this panel for every new lookup
 * (and FileViewer for every file), so earlier results, errors and pending requests never
 * outlive their lookup. The server's bases and subclasses are shown as returned: never
 * re-resolved, sorted, deduplicated or filtered.
 */
export function CodeTypeHierarchy({
  projectId,
  repositoryId,
  definitions,
  onSelectLine,
}: CodeTypeHierarchyProps) {
  const hierarchy = useCodeTypeHierarchy(projectId, repositoryId)
  // Server order. Only classes have a type hierarchy; nothing else is ever requested.
  const targets = (definitions ?? [])
    .map((definition, index) => ({ definition, index }))
    .filter(({ definition }) => definition.kind === 'class')
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
    <section className="space-y-2 border-t border-slate-800 pt-2" aria-label="Type hierarchy">
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
                ? `Loading type hierarchy for ${identity(definition)}…`
                : `Type hierarchy for ${identity(definition)}`}
            </button>
          )
        })}
      </div>
      {hierarchy.isError && (
        <ErrorMessage
          message={getErrorMessage(hierarchy.error, 'Could not load the type hierarchy')}
        />
      )}
      {subject && (
        <p className={NOTE} data-testid="type-hierarchy-target">
          {`Type hierarchy of ${identity(subject)}`}
        </p>
      )}
      {unavailable && <p className={NOTE}>{unavailable}</p>}
      {result && scanned && result.target && (
        <div className="space-y-2 text-xs">
          <p className="text-slate-500" data-testid="type-hierarchy-summary">
            {hierarchySummary(result.summary)}
          </p>
          <h4 className="font-semibold text-slate-300">Bases</h4>
          {result.bases.length === 0 ? (
            <p className={NOTE}>{NO_BASES}</p>
          ) : (
            <ul className="space-y-1" aria-label="Bases">
              {/* Server (source) order; never re-sorted, deduplicated or filtered here. */}
              {result.bases.map((base, index) => (
                <Base
                  key={`${index}:${base.site?.line ?? ''}:${base.site?.column ?? ''}`}
                  base={base}
                  targetPath={result.target?.path ?? ''}
                  onSelectLine={onSelectLine}
                />
              ))}
            </ul>
          )}
          {result.bases_truncated === true && <p className={NOTE}>{BASES_TRUNCATED}</p>}
          <h4 className="font-semibold text-slate-300">Direct subclasses</h4>
          {result.subclasses_status === 'unsupported_target' || result.subclasses === null ? (
            <p className={NOTE}>{SUBCLASSES_UNSUPPORTED}</p>
          ) : result.subclasses.length === 0 ? (
            <p className={NOTE}>{NO_SUBCLASSES}</p>
          ) : (
            <ul className="space-y-1" aria-label="Direct subclasses">
              {result.subclasses.map((entry, index) => (
                <Subclass
                  key={`${index}:${entry.subclass.path}:${entry.site.line}:${entry.site.column}`}
                  entry={entry}
                  onSelectLine={onSelectLine}
                />
              ))}
            </ul>
          )}
          {result.subclasses_truncated === true && <p className={NOTE}>{SUBCLASSES_TRUNCATED}</p>}
        </div>
      )}
    </section>
  )
}
