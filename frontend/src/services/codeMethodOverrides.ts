import type { MethodOverridesResult } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/**
 * Direct overrides of the method statement starting on ``line`` of ``path``, found by the
 * server. Only the path and the line are sent, as query parameters (encoded by axios).
 */
export async function getCodeMethodOverrides(
  projectId: string,
  repositoryId: string,
  path: string,
  line: number,
): Promise<MethodOverridesResult> {
  const { data } = await apiClient.get<MethodOverridesResult>(
    `${repositoryPath(projectId, repositoryId)}/files/method-overrides`,
    { params: { path, line } },
  )
  return data
}
