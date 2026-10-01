import type { AnalysisJob } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/** The repository's jobs, newest first (server order). */
export async function listJobs(projectId: string, repositoryId: string): Promise<AnalysisJob[]> {
  const { data } = await apiClient.get<AnalysisJob[]>(
    `${repositoryPath(projectId, repositoryId)}/jobs`,
  )
  return data
}
