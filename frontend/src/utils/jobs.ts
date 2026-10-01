import type { AnalysisJob } from '../types/api'

export const JOB_POLL_INTERVAL_MS = 2000

export function isJobActive(job: AnalysisJob | undefined): boolean {
  return job?.status === 'pending' || job?.status === 'processing'
}

/** Poll only while the latest (first) job is pending or processing. */
export function jobRefetchInterval(
  jobs: AnalysisJob[] | undefined,
  intervalMs: number = JOB_POLL_INTERVAL_MS,
): number | false {
  return isJobActive(jobs?.[0]) ? intervalMs : false
}
