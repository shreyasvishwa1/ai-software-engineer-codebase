import { useCodeMetrics, useStartCodeMetrics } from '../hooks/useCodeMetrics'
import { getErrorMessage } from '../services/client'
import type { CodeMetricsSummary, FileMetric, RepositoryStatus } from '../types/api'
import { isJobActive } from '../utils/jobs'
import { ErrorMessage } from './ErrorMessage'
import { StatusBadge } from './StatusBadge'

export const TOP_FILES = 10
const NOTE = 'text-sm text-slate-400'

const SUMMARY_ROWS: [keyof CodeMetricsSummary, string][] = [
  ['files', 'Files'],
  ['analyzed', 'Analyzed'],
  ['binary', 'Binary'],
  ['too_large', 'Too large'],
  ['unavailable', 'Unavailable'],
  ['total_lines', 'Total lines'],
  ['blank_lines', 'Blank lines'],
]

/** Files with a line count, most lines first; ties by path (code-point order). */
export function largestFiles(files: FileMetric[], limit: number = TOP_FILES): FileMetric[] {
  return files
    .filter((file) => file.line_count !== null)
    .sort(
      (a, b) =>
        (b.line_count ?? 0) - (a.line_count ?? 0) ||
        (a.path < b.path ? -1 : a.path > b.path ? 1 : 0),
    )
    .slice(0, limit)
}

interface CodeMetricsPanelProps {
  projectId: string
  repositoryId: string
  repositoryStatus: RepositoryStatus
  /** When given, largest-file paths become buttons that open the file in the viewer. */
  onSelectFile?: (path: string) => void
}

/** Start a code-metrics analysis and show the results for the repository's current files. */
export function CodeMetricsPanel({
  projectId,
  repositoryId,
  repositoryStatus,
  onSelectFile,
}: CodeMetricsPanelProps) {
  const metrics = useCodeMetrics(projectId, repositoryId)
  const start = useStartCodeMetrics(projectId, repositoryId)
  const job = metrics.data?.job ?? null
  const active = isJobActive(job ?? undefined)
  const ready = repositoryStatus === 'ready'
  const disabled = !ready || active || start.isPending || metrics.isPending

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={disabled}
          onClick={() => start.mutate()}
          className="rounded-lg bg-indigo-500 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-400 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Analyze code
        </button>
        {job && <StatusBadge status={job.status} />}
      </div>
      {!ready && (
        <p className={NOTE}>The repository must be ingested successfully before it can be analyzed.</p>
      )}
      {start.isError && (
        <ErrorMessage message={getErrorMessage(start.error, 'Could not start the analysis')} />
      )}
      {metrics.isPending && <p className={NOTE}>Loading code metrics…</p>}
      {metrics.isError && (
        <ErrorMessage message={getErrorMessage(metrics.error, 'Could not load code metrics')} />
      )}
      {metrics.data && (
        <>
          {active && <p className={NOTE}>Analysis in progress…</p>}
          {job?.status === 'failed' && job.error_message && (
            <ErrorMessage message={job.error_message} />
          )}
          {metrics.data.summary ? (
            <Results
              summary={metrics.data.summary}
              files={metrics.data.files}
              onSelectFile={onSelectFile}
            />
          ) : (
            !active && (
              <p className={NOTE}>{job ? 'Run the analysis for the current files.' : 'No analysis yet.'}</p>
            )
          )}
        </>
      )}
    </div>
  )
}

function Results({
  summary,
  files,
  onSelectFile,
}: {
  summary: CodeMetricsSummary
  files: FileMetric[]
  onSelectFile?: (path: string) => void
}) {
  const largest = largestFiles(files)
  return (
    <div className="space-y-4">
      <table className="w-full text-sm" aria-label="Code metrics summary">
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
      {largest.length > 0 && (
        <table className="w-full text-sm" aria-label="Largest files">
          <thead>
            <tr className="text-left text-xs text-slate-500">
              <th className="py-1 font-normal">File</th>
              <th className="py-1 font-normal">Language</th>
              <th className="py-1 text-right font-normal">Lines</th>
              <th className="py-1 text-right font-normal">Blank</th>
            </tr>
          </thead>
          <tbody>
            {largest.map((file) => (
              <tr key={file.path} className="border-t border-slate-800">
                <td className="break-all py-1 font-mono text-xs text-slate-200">
                  {onSelectFile ? (
                    <button
                      type="button"
                      aria-label={`Open ${file.path} in the viewer`}
                      onClick={() => onSelectFile(file.path)}
                      className="break-all text-left text-indigo-300 hover:underline"
                    >
                      {file.path}
                    </button>
                  ) : (
                    file.path
                  )}
                </td>
                <td className="py-1 text-xs text-slate-400">{file.language ?? 'Unknown'}</td>
                <td className="py-1 text-right text-slate-200">{file.line_count}</td>
                <td className="py-1 text-right text-slate-400">{file.blank_line_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
