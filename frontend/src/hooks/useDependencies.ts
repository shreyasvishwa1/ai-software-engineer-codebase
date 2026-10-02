import { useQuery } from '@tanstack/react-query'

import { getDependencies } from '../services/dependencies'
import { queryKeys } from './queryKeys'

/** The repository's dependency inventory. Not polled; useJobs refreshes it when a job finishes. */
export function useDependencies(projectId: string, repositoryId: string) {
  return useQuery({
    queryKey: queryKeys.dependencies(projectId, repositoryId),
    queryFn: () => getDependencies(projectId, repositoryId),
  })
}
