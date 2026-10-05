import { useRef, useState } from 'react'

import { useCodeOutline } from '../hooks/useCodeOutline'
import { getErrorMessage } from '../services/client'
import type { DefinitionLocation, OutlineStatus, OutlineSymbol } from '../types/api'
import { CodeCallHierarchy } from './CodeCallHierarchy'
import { CodeMethodOverrides } from './CodeMethodOverrides'
import { CodeReferences } from './CodeReferences'
import { CodeTypeHierarchy } from './CodeTypeHierarchy'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const EMPTY_OUTLINE = 'No classes or functions found.'
export const TRUNCATED_NOTICE =
  'The outline is incomplete: at most 2,000 symbols and 8 nesting levels are shown.'
export const UNKNOWN_STATUS = 'The outline is unavailable.'
export const STATUS_MESSAGES: Record<Exclude<OutlineStatus, 'parsed'>, string> = {
  syntax_error: 'The outline is unavailable: this file could not be parsed as Python.',
  too_large: 'The outline is unavailable: this file is too large to outline (limit 256 KiB).',
  binary: 'The outline is unavailable: this file is not text.',
  unsupported_language: 'The outline is only available for Python files.',
}
const MAX_INDENT = 8
/** The outline kinds the existing definition analyses accept as targets. */
const ANALYZABLE_KINDS: ReadonlySet<string> = new Set(['class', 'function', 'method'])

/** One click on an outline symbol: the analysis context of that symbol. */
interface SymbolSelection {
  /** A new token for every click, so even the same symbol starts a fresh context. */
  generation: number
  /** The symbol's position in the outline response. */
  index: number
  /** The symbol as a definition of the outline response's (canonical) file. */
  target: DefinitionLocation
}

interface FileOutlineProps {
  projectId: string
  repositoryId: string
  /** The open file's server-provided path. */
  path: string
  language: string | null
  /** When given, every symbol becomes a button that selects its line in the viewer. */
  onSelectLine?: (path: string, line: number) => void
}

function isLine(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 1
}

function indent(depth: unknown): number {
  return typeof depth === 'number' && Number.isInteger(depth) && depth > 0
    ? Math.min(depth, MAX_INDENT)
    : 0
}

/** Only the symbol's named fields, always as plain text. */
function SymbolLabel({ symbol }: { symbol: OutlineSymbol }) {
  return (
    <>
      <span className="text-slate-500">{symbol.kind}</span>
      {symbol.async === true && <span className="text-amber-300">async</span>}
      <span className="break-all font-mono text-slate-200">{symbol.name}</span>
      <span className="ml-auto text-slate-500">{`line ${symbol.line}–${symbol.end_line}`}</span>
    </>
  )
}

/**
 * On-demand outline of the displayed Python file. Nothing is requested until the button is
 * clicked; the component is keyed by path, so another file always starts fresh. Every
 * server value is rendered as plain text. Clicking a symbol selects its line and opens the
 * existing definition analyses for it below the list; each still requests only when its own
 * button is clicked, and every click (and every new outline) starts a fresh context.
 */
export function FileOutline({
  projectId,
  repositoryId,
  path,
  language,
  onSelectLine,
}: FileOutlineProps) {
  const outline = useCodeOutline(projectId, repositoryId)
  const [selection, setSelection] = useState<SymbolSelection | null>(null)
  const clicks = useRef(0)
  if (language !== 'Python') {
    return null
  }
  const result = outline.isSuccess ? outline.data : undefined
  const unavailable =
    result && result.status !== 'parsed'
      ? (STATUS_MESSAGES[result.status as keyof typeof STATUS_MESSAGES] ?? UNKNOWN_STATUS)
      : null

  /** A clicked symbol of the outline at `outlinePath`: analyzable kinds only, else nothing. */
  const select = (index: number, symbol: OutlineSymbol, outlinePath: string) => {
    clicks.current += 1
    setSelection(
      ANALYZABLE_KINDS.has(symbol.kind)
        ? {
            generation: clicks.current,
            index,
            target: {
              path: outlinePath,
              kind: symbol.kind,
              name: symbol.name,
              line: symbol.line,
              end_line: symbol.end_line,
            },
          }
        : null,
    )
  }

  return (
    <section className="space-y-2 rounded-lg border border-slate-800 p-3" aria-label="File outline">
      <button
        type="button"
        disabled={outline.isPending}
        onClick={() => {
          // A new outline replaces the symbols: no analysis context survives the request.
          setSelection(null)
          outline.mutate(path)
        }}
        className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {outline.isPending ? 'Loading outline…' : result ? 'Refresh outline' : 'Show outline'}
      </button>
      {outline.isError && (
        <ErrorMessage message={getErrorMessage(outline.error, 'Could not load the outline')} />
      )}
      {unavailable && <p className={NOTE}>{unavailable}</p>}
      {result && result.status === 'parsed' && (
        <>
          {result.truncated === true && <p className={NOTE}>{TRUNCATED_NOTICE}</p>}
          {result.symbols.length === 0 ? (
            <p className={NOTE}>{EMPTY_OUTLINE}</p>
          ) : (
            // Server order (source order); never re-sorted here.
            <ul className="space-y-0.5 text-xs" aria-label="Outline">
              {result.symbols.map((symbol, index) => (
                <li
                  key={`${index}:${symbol.line}`}
                  style={{ paddingLeft: `${indent(symbol.depth)}rem` }}
                  data-testid="outline-symbol"
                >
                  {onSelectLine && isLine(symbol.line) ? (
                    <button
                      type="button"
                      aria-label={`Go to ${symbol.kind} ${symbol.name} (line ${symbol.line})`}
                      onClick={() => {
                        onSelectLine(path, symbol.line)
                        select(index, symbol, result.path)
                      }}
                      className="flex w-full flex-wrap items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-slate-800"
                    >
                      <SymbolLabel symbol={symbol} />
                    </button>
                  ) : (
                    <div className="flex flex-wrap items-center gap-2 px-1 py-0.5">
                      <SymbolLabel symbol={symbol} />
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      {selection && result?.status === 'parsed' && (
        // Keyed per click: every selection, even of the same symbol, mounts fresh panels.
        <section
          key={`${selection.generation}:${selection.index}:${selection.target.line}:${selection.target.kind}`}
          className="space-y-2 border-t border-slate-800 pt-2"
          aria-label="Symbol analyses"
        >
          <p className={NOTE} data-testid="symbol-analyses-subject">
            {`Analyses for ${selection.target.kind} ${selection.target.name} (line ${selection.target.line})`}
          </p>
          {/* The existing panels, unchanged: each filters by kind and requests on its own click. */}
          <CodeReferences
            projectId={projectId}
            repositoryId={repositoryId}
            definitions={[selection.target]}
            onSelectLine={onSelectLine}
          />
          <CodeCallHierarchy
            projectId={projectId}
            repositoryId={repositoryId}
            definitions={[selection.target]}
            onSelectLine={onSelectLine}
          />
          <CodeTypeHierarchy
            projectId={projectId}
            repositoryId={repositoryId}
            definitions={[selection.target]}
            onSelectLine={onSelectLine}
          />
          <CodeMethodOverrides
            projectId={projectId}
            repositoryId={repositoryId}
            definitions={[selection.target]}
            onSelectLine={onSelectLine}
          />
        </section>
      )}
    </section>
  )
}
