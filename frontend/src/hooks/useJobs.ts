import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'

import { listJobs } from '../services/jobs'
import { isJobActive, JOB_POLL_INTERVAL_MS, jobRefetchInterval } from '../utils/jobs'
import { queryKeys } from './queryKeys'

/**
 * The repository's jobs, polled only while the latest job is pending or processing. When a
 * job finishes (completed or failed), the repository, tree, languages, jobs and code metrics
 * are refreshed.
 */
export function useJobs(
  projectId: string,
  repositoryId: string,
  pollIntervalMs: number = JOB_POLL_INTERVAL_MS,
) {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: queryKeys.jobs(projectId, repositoryId),
    queryFn: () => listJobs(projectId, repositoryId),
    refetchInterval: (q) => jobRefetchInterval(q.state.data, pollIntervalMs),
  })

  const latest = query.data?.[0]
  const wasActive = useRef(false)
  useEffect(() => {
    const active = isJobActive(latest)
    if (wasActive.current && !active && latest) {
      for (const key of [
        queryKeys.repository(projectId, repositoryId),
        queryKeys.tree(projectId, repositoryId),
        queryKeys.languages(projectId, repositoryId),
        queryKeys.jobs(projectId, repositoryId),
        queryKeys.codeMetrics(projectId, repositoryId),
        queryKeys.dependencies(projectId, repositoryId),
      ]) {
        void queryClient.invalidateQueries({ queryKey: key, exact: true })
      }
      void queryClient.invalidateQueries({ queryKey: queryKeys.repositories(projectId) })
    }
    wasActive.current = active
  }, [latest, projectId, repositoryId, queryClient])

  return query
}
