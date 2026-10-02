import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { getCodeMetrics, startCodeMetrics } from '../services/codeMetrics'
import { queryKeys } from './queryKeys'

/**
 * The repository's code metrics. Not polled here: while an analysis runs, useJobs polls the
 * job list and refreshes this query when the job finishes.
 */
export function useCodeMetrics(projectId: string, repositoryId: string) {
  return useQuery({
    queryKey: queryKeys.codeMetrics(projectId, repositoryId),
    queryFn: () => getCodeMetrics(projectId, repositoryId),
  })
}

/** Start an analysis; on success the code metrics and the job list (polling) are refreshed. */
export function useStartCodeMetrics(projectId: string, repositoryId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => startCodeMetrics(projectId, repositoryId),
    onSuccess: () => {
      for (const key of [
        queryKeys.codeMetrics(projectId, repositoryId),
        queryKeys.jobs(projectId, repositoryId),
      ]) {
        void queryClient.invalidateQueries({ queryKey: key, exact: true })
      }
    },
  })
}
