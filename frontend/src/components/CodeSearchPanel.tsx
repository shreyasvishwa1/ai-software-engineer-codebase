import { useState, type FormEvent } from 'react'

import { useCodeSearch } from '../hooks/useCodeSearch'
import { getErrorMessage } from '../services/client'
import type { CodeSearchSummary } from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const HINT = 'Literal, case-insensitive text search.'
export const NO_MATCHES = 'No matches found.'
export const TRUNCATED_NOTICE =
  'Results are incomplete: the search stopped at a limit (at most 5,000 files, 64 MiB or 200 matches).'

const SUMMARY_ROWS: [keyof CodeSearchSummary, string][] = [
  ['files_scanned', 'Files scanned'],
  ['binary', 'Binary or not UTF-8'],
  ['too_large', 'Too large'],
  ['unavailable', 'Unavailable'],
  ['matches', 'Matches'],
]

/**
 * On-demand repository search: the server does all matching and validation; a request is
 * sent only when the form is submitted. Every server value is rendered as plain text.
 */
export function CodeSearchPanel({
  projectId,
  repositoryId,
  onSelectFile,
}: {
  projectId: string
  repositoryId: string
  /** When given, every result gets a "View file" button that opens it in the viewer. */
  onSelectFile?: (path: string) => void
}) {
  const search = useCodeSearch(projectId, repositoryId)
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
      <form onSubmit={onSubmit} aria-label="Search code" className="flex flex-wrap items-end gap-2">
        <label className="block min-w-0 flex-1 text-sm">
          <span className="text-slate-400">Search text</span>
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
      {search.isPending && <p className={NOTE}>Searching the repository…</p>}
      {search.isError && (
        <ErrorMessage message={getErrorMessage(search.error, 'Could not search the repository')} />
      )}
      {result && (
        <div className="space-y-4">
          <p className={NOTE}>
            Results for <span className="whitespace-pre-wrap break-all font-mono text-slate-200">{search.variables}</span>
          </p>
          <table className="w-full text-sm" aria-label="Search summary">
            <tbody>
              {SUMMARY_ROWS.map(([key, label]) => (
                <tr key={key} className="border-b border-slate-800 last:border-0">
                  <th scope="row" className="py-1 text-left font-normal text-slate-400">
                    {label}
                  </th>
                  <td className="py-1 text-right text-slate-200">{result.summary[key]}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {result.truncated && <p className={NOTE}>{TRUNCATED_NOTICE}</p>}
          {result.matches.length === 0 ? (
            <p className={NOTE}>{NO_MATCHES}</p>
          ) : (
            // Rows stay in the server's order (path, line); never re-sorted here.
            <div className="overflow-x-auto">
              <table className="w-full text-xs" aria-label="Search results">
                <thead>
                  <tr className="text-left text-slate-500">
                    <th className="py-1 font-normal">File</th>
                    <th className="py-1 font-normal">Line</th>
                    <th className="py-1 font-normal">Match</th>
                    {onSelectFile && (
                      <th className="py-1 font-normal">
                        <span className="sr-only">Actions</span>
                      </th>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {result.matches.map((match, index) => (
                    <tr key={`${match.path}:${match.line}:${index}`} className="border-t border-slate-800 align-top">
                      <td className="break-all py-1 pr-2 font-mono text-slate-200">{match.path}</td>
                      <td className="py-1 pr-2 text-slate-300">{match.line}</td>
                      <td className="py-1 pr-2">
                        <code className="whitespace-pre font-mono text-slate-300" data-testid="search-preview">
                          {match.preview}
                        </code>
                      </td>
                      {onSelectFile && (
                        <td className="py-1 text-right">
                          <button
                            type="button"
                            aria-label={`Open ${match.path} in the viewer (line ${match.line})`}
                            onClick={() => onSelectFile(match.path)}
                            className="shrink-0 whitespace-nowrap rounded border border-slate-700 px-2 py-0.5 text-xs text-slate-200 hover:bg-slate-800"
                          >
                            View file
                          </button>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
