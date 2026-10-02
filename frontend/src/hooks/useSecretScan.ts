import { useQuery } from '@tanstack/react-query'

import { getSecretScan } from '../services/secretScan'
import { queryKeys } from './queryKeys'

/** The repository's secret scan. Not polled; useJobs refreshes it when a job finishes. */
export function useSecretScan(projectId: string, repositoryId: string) {
  return useQuery({
    queryKey: queryKeys.secretScan(projectId, repositoryId),
    queryFn: () => getSecretScan(projectId, repositoryId),
  })
}
