import type { CodeMetrics, CodeMetricsJob } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

function codeMetricsPath(projectId: string, repositoryId: string): string {
  return `${repositoryPath(projectId, repositoryId)}/analysis/code-metrics`
}

/** The latest code-metrics job and the metrics of the repository's current files. */
export async function getCodeMetrics(projectId: string, repositoryId: string): Promise<CodeMetrics> {
  const { data } = await apiClient.get<CodeMetrics>(codeMetricsPath(projectId, repositoryId))
  return data
}

/** Start a code-metrics analysis; returns the queued job. */
export async function startCodeMetrics(
  projectId: string,
  repositoryId: string,
): Promise<CodeMetricsJob> {
  const { data } = await apiClient.post<CodeMetricsJob>(codeMetricsPath(projectId, repositoryId))
  return data
}
