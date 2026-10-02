import { useState } from 'react'

import { useCancelJob } from '../hooks/useCancelJob'
import { useJobs } from '../hooks/useJobs'
import { getErrorMessage } from '../services/client'
import type { AnalysisJob, AnalysisJobType } from '../types/api'
import { formatDateTime } from '../utils/format'
import { JOB_POLL_INTERVAL_MS } from '../utils/jobs'
import { ErrorMessage } from './ErrorMessage'
import { StatusBadge } from './StatusBadge'

const JOB_TYPE_LABELS: Record<AnalysisJobType, string> = {
  ingestion: 'Ingestion',
  code_metrics: 'Code analysis',
}

interface CancelButtonProps {
  projectId: string
  repositoryId: string
  job: AnalysisJob
  onError: (message: string | null) => void
}

/** Cancel one pending job; only this row's button is disabled while its request runs. */
function CancelButton({ projectId, repositoryId, job, onError }: CancelButtonProps) {
  const cancel = useCancelJob(projectId, repositoryId)
  const label = (JOB_TYPE_LABELS[job.job_type] ?? job.job_type).toLowerCase()
  return (
    <button
      type="button"
      aria-label={`Cancel ${label} job`}
      disabled={cancel.isPending}
      onClick={() => {
        onError(null)
        cancel.mutate(job.id, {
          onError: (error) => onError(getErrorMessage(error, 'Could not cancel the job')),
        })
      }}
      className="rounded border border-slate-700 px-2 py-0.5 text-xs text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
    >
      Cancel
    </button>
  )
}

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
  // Kept here, not in the row: the row disappears once the refreshed job is no longer pending.
  const [cancelError, setCancelError] = useState<string | null>(null)
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
        <span className="text-xs text-slate-400" data-testid="job-type">
          {JOB_TYPE_LABELS[latest.job_type] ?? latest.job_type}
        </span>
        <StatusBadge status={latest.status} />
        <span className="text-xs text-slate-400" data-testid="job-progress">
          {latest.progress}%
        </span>
        {latest.status === 'pending' && (
          <CancelButton
            projectId={projectId}
            repositoryId={repositoryId}
            job={latest}
            onError={setCancelError}
          />
        )}
      </div>
      {cancelError && <ErrorMessage message={cancelError} />}
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
              <span>{JOB_TYPE_LABELS[job.job_type] ?? job.job_type}</span>
              <span>{formatDateTime(job.created_at)}</span>
              {job.error_message && <span className="text-red-300">{job.error_message}</span>}
              {job.status === 'pending' && (
                <CancelButton
                  projectId={projectId}
                  repositoryId={repositoryId}
                  job={job}
                  onError={setCancelError}
                />
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
