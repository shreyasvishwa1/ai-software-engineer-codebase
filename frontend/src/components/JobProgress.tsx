import { useJobs } from '../hooks/useJobs'
import { getErrorMessage } from '../services/client'
import { formatDateTime } from '../utils/format'
import { JOB_POLL_INTERVAL_MS } from '../utils/jobs'
import { ErrorMessage } from './ErrorMessage'
import { StatusBadge } from './StatusBadge'

interface JobProgressProps {
  projectId: string
  repositoryId: string
  pollIntervalMs?: number
}

export function JobProgress({
  projectId,
  repositoryId,
  pollIntervalMs = JOB_POLL_INTERVAL_MS,
}: JobProgressProps) {
  const jobs = useJobs(projectId, repositoryId, pollIntervalMs)
  if (jobs.isPending) {
    return <p className="text-sm text-slate-400">Loading jobs…</p>
  }
  if (jobs.isError) {
    return <ErrorMessage message={getErrorMessage(jobs.error, 'Could not load jobs')} />
  }
  const [latest, ...older] = jobs.data
  if (!latest) {
    return <p className="text-sm text-slate-400">No ingestion jobs yet.</p>
  }
  return (
    <section className="space-y-3" aria-label="Ingestion progress">
      <div className="flex items-center gap-3">
        <h2 className="text-sm font-semibold text-slate-200">Latest job</h2>
        <StatusBadge status={latest.status} />
        <span className="text-xs text-slate-400" data-testid="job-progress">
          {latest.progress}%
        </span>
      </div>
      <div className="h-1.5 rounded bg-slate-800">
        <div className="h-1.5 rounded bg-emerald-500" style={{ width: `${latest.progress}%` }} />
      </div>
      {latest.error_message && <ErrorMessage message={latest.error_message} />}
      <p className="text-xs text-slate-500">
        Created {formatDateTime(latest.created_at)} · Finished {formatDateTime(latest.completed_at)}
      </p>
      {older.length > 0 && (
        <ul className="space-y-1 text-xs text-slate-400" aria-label="Recent jobs">
          {older.slice(0, 5).map((job) => (
            <li key={job.id} className="flex items-center gap-2">
              <StatusBadge status={job.status} />
              <span>{formatDateTime(job.created_at)}</span>
              {job.error_message && <span className="text-red-300">{job.error_message}</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
