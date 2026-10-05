import type { UnusedVariablesResult } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/**
 * Assignments of one Python file to def/async def locals that nothing in the file reads (found by
 * the server). The path is sent as a query parameter (encoded by axios), never concatenated into
 * the URL.
 */
export async function getCodeUnusedVariables(
  projectId: string,
  repositoryId: string,
  path: string,
): Promise<UnusedVariablesResult> {
  const { data } = await apiClient.get<UnusedVariablesResult>(
    `${repositoryPath(projectId, repositoryId)}/files/unused-variables`,
    { params: { path } },
  )
  return data
}
