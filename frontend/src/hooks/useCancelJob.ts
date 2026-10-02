import { useMutation, useQueryClient } from '@tanstack/react-query'

import { cancelJob } from '../services/jobs'
import type { AnalysisJob } from '../types/api'
import { queryKeys } from './queryKeys'

/**
 * Cancel one pending job. The returned job (server state) replaces it in the cached list;
 * afterwards the jobs and code-metrics queries are refreshed, whether or not the request
 * succeeded. Polling stays with useJobs.
 */
export function useCancelJob(projectId: string, repositoryId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (jobId: string) => cancelJob(projectId, repositoryId, jobId),
    onSuccess: (job) => {
      queryClient.setQueryData<AnalysisJob[]>(queryKeys.jobs(projectId, repositoryId), (jobs) =>
        jobs?.map((current) => (current.id === job.id ? job : current)),
      )
    },
    onSettled: () => {
      for (const key of [
        queryKeys.jobs(projectId, repositoryId),
        queryKeys.codeMetrics(projectId, repositoryId),
      ]) {
        void queryClient.invalidateQueries({ queryKey: key, exact: true })
      }
    },
  })
}
