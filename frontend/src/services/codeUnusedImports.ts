import type { UnusedImportsResult } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/**
 * Imports of one Python file whose bound name nothing in the file reads (found by the server). The
 * path is sent as a query parameter (encoded by axios), never concatenated into the URL.
 */
export async function getCodeUnusedImports(
  projectId: string,
  repositoryId: string,
  path: string,
): Promise<UnusedImportsResult> {
  const { data } = await apiClient.get<UnusedImportsResult>(
    `${repositoryPath(projectId, repositoryId)}/files/unused-imports`,
    { params: { path } },
  )
  return data
}
