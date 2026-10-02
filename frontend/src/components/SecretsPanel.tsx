import { useSecretScan } from '../hooks/useSecretScan'
import { getErrorMessage } from '../services/client'
import type { SecretScanSummary } from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const CLEAN_MESSAGE = 'No likely secrets found.'
export const CAVEAT =
  'Pattern-based scan for a few common credential formats; no findings does not guarantee there are no secrets.'
export const TRUNCATED_NOTICE =
  'Results are incomplete: the scan stopped at a limit (at most 5,000 files, 64 MiB or 500 findings).'

const SUMMARY_ROWS: [keyof SecretScanSummary, string][] = [
  ['files_scanned', 'Files scanned'],
  ['binary', 'Binary or not UTF-8'],
  ['too_large', 'Too large'],
  ['unavailable', 'Unavailable'],
  ['findings', 'Findings'],
]

/**
 * Where the server found likely secrets: path, line and type only. The server never sends
 * the matched values and only those named fields are rendered, always as plain text.
 */
export function SecretsPanel({
  projectId,
  repositoryId,
  onSelectFile,
}: {
  projectId: string
  repositoryId: string
  /** When given, every finding gets a "View file" button that opens it in the viewer. */
  onSelectFile?: (path: string) => void
}) {
  const scan = useSecretScan(projectId, repositoryId)
  if (scan.isPending) {
    return <p className={NOTE}>Scanning for secrets…</p>
  }
  if (scan.isError) {
    return <ErrorMessage message={getErrorMessage(scan.error, 'Could not scan for secrets')} />
  }
  const { findings, summary, truncated } = scan.data
  return (
    <div className="space-y-4">
      <table className="w-full text-sm" aria-label="Secret scan summary">
        <tbody>
          {SUMMARY_ROWS.map(([key, label]) => (
            <tr key={key} className="border-b border-slate-800 last:border-0">
              <th scope="row" className="py-1 text-left font-normal text-slate-400">
                {label}
              </th>
              <td className="py-1 text-right text-slate-200">{summary[key]}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {truncated && <p className={NOTE}>{TRUNCATED_NOTICE}</p>}
      {findings.length === 0 ? (
        <p className={NOTE}>{CLEAN_MESSAGE}</p>
      ) : (
        // Rows stay in the server's order (path, line, rule); never re-sorted here.
        <table className="w-full text-xs" aria-label="Secret findings">
          <thead>
            <tr className="text-left text-slate-500">
              <th className="py-1 font-normal">File</th>
              <th className="py-1 font-normal">Line</th>
              <th className="py-1 font-normal">Type</th>
              {onSelectFile && (
                <th className="py-1 font-normal">
                  <span className="sr-only">Actions</span>
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {findings.map((finding, index) => (
              <tr
                key={`${finding.path}:${finding.line}:${finding.rule}:${index}`}
                className="border-t border-slate-800"
              >
                <td className="break-all py-1 font-mono text-slate-200">{finding.path}</td>
                <td className="py-1 text-slate-300">{finding.line}</td>
                <td className="py-1 text-slate-300">
                  {finding.description}
                  <span className="ml-2 font-mono text-slate-500">{finding.rule}</span>
                </td>
                {onSelectFile && (
                  <td className="py-1 text-right">
                    <button
                      type="button"
                      aria-label={`Open ${finding.path} in the viewer (line ${finding.line}, ${finding.description})`}
                      onClick={() => onSelectFile(finding.path)}
                      className="shrink-0 rounded border border-slate-700 px-2 py-0.5 text-xs text-slate-200 hover:bg-slate-800"
                    >
                      View file
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="text-xs text-slate-500">{CAVEAT}</p>
    </div>
  )
}
