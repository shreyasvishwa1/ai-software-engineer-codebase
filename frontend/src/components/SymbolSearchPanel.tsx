import { useState, type FormEvent } from 'react'

import { useSymbolSearch } from '../hooks/useSymbolSearch'
import { getErrorMessage } from '../services/client'
import type { SymbolMatch, SymbolSearchSummary } from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const HINT = 'Exact Python class, function or method name (case-sensitive).'
export const NO_SYMBOLS = 'No symbols found.'
export const TRUNCATED_NOTICE =
  'Results may be incomplete: a scan or result limit was reached (at most 200 results).'

export function summaryText(summary: SymbolSearchSummary): string {
  return `Scanned ${summary.files_scanned} files · ${summary.too_large} too large · ${summary.binary} binary · ${summary.syntax_error} with syntax errors.`
}

function isLine(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 1
}

/** One result: only its named fields, always as plain text. */
function ResultLabel({ result }: { result: SymbolMatch }) {
  return (
    <>
      <span className="text-slate-500">{result.kind}</span>
      {result.async === true && <span className="text-amber-300">async</span>}
      <span className="break-all font-mono text-slate-200">{result.name}</span>
      {typeof result.depth === 'number' && result.depth > 0 && <span className="text-slate-500">nested</span>}
      <span className="whitespace-pre-wrap break-all font-mono text-slate-400">{result.path}</span>
      <span className="ml-auto text-slate-500">{`line ${result.line}–${result.end_line}`}</span>
    </>
  )
}

/**
 * On-demand Python symbol search: the server does all matching and validation; a request
 * is sent only when the form is submitted. Results keep the server's order.
 */
export function SymbolSearchPanel({
  projectId,
  repositoryId,
  onSelectLine,
}: {
  projectId: string
  repositoryId: string
  /** When given, every result becomes a button that opens its file at its line. */
  onSelectLine?: (path: string, line: number) => void
}) {
  const search = useSymbolSearch(projectId, repositoryId)
  const [query, setQuery] = useState('')
  const result = search.isSuccess ? search.data : undefined

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (query === '' || search.isPending) {
      return
    }
    search.mutate(query) // exactly as typed: the server validates it
  }

  return (
    <div className="space-y-4">
      <form onSubmit={onSubmit} aria-label="Search symbols" className="flex flex-wrap items-end gap-2">
        <label className="block min-w-0 flex-1 text-sm">
          <span className="text-slate-400">Symbol name</span>
          <input
            type="text"
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <button
          type="submit"
          disabled={query === '' || search.isPending}
          className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {search.isPending ? 'Searching…' : 'Search'}
        </button>
      </form>
      {search.isIdle && <p className={NOTE}>{HINT}</p>}
      {search.isPending && <p className={NOTE}>Searching symbols…</p>}
      {search.isError && (
        <ErrorMessage message={getErrorMessage(search.error, 'Could not search symbols')} />
      )}
      {result && (
        <div className="space-y-3">
          <p className={NOTE}>
            Results for <span className="whitespace-pre-wrap break-all font-mono text-slate-200">{search.variables}</span>
          </p>
          <p className="text-xs text-slate-500" data-testid="symbols-summary">
            {summaryText(result.summary)}
          </p>
          {result.truncated === true && <p className={NOTE}>{TRUNCATED_NOTICE}</p>}
          {result.results.length === 0 ? (
            <p className={NOTE}>{NO_SYMBOLS}</p>
          ) : (
            // Server order (path, then source order); never re-sorted or filtered here.
            <ul className="space-y-0.5 text-xs" aria-label="Symbol results">
              {result.results.map((match, index) => (
                <li key={`${index}:${match.path}:${match.line}`} data-testid="symbol-result">
                  {onSelectLine && isLine(match.line) ? (
                    <button
                      type="button"
                      aria-label={`Open ${match.path} at line ${match.line} (${match.kind} ${match.name})`}
                      onClick={() => onSelectLine(match.path, match.line)}
                      className="flex w-full flex-wrap items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-slate-800"
                    >
                      <ResultLabel result={match} />
                    </button>
                  ) : (
                    <div className="flex flex-wrap items-center gap-2 px-1 py-0.5">
                      <ResultLabel result={match} />
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
