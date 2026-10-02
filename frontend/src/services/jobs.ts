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

/** Cancel a pending job; returns the updated job. The server decides (409 when not pending). */
export async function cancelJob(
  projectId: string,
  repositoryId: string,
  jobId: string,
): Promise<AnalysisJob> {
  const { data } = await apiClient.post<AnalysisJob>(
    `${repositoryPath(projectId, repositoryId)}/jobs/${encodeURIComponent(jobId)}/cancel`,
  )
  return data
}
