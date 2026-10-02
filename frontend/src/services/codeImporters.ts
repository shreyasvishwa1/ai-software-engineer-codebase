import type { CodeImporters } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/**
 * Python files whose imports resolve to one file (computed by the server). The path is sent
 * as a query parameter (encoded by axios), never concatenated into the URL.
 */
export async function getCodeImporters(
  projectId: string,
  repositoryId: string,
  path: string,
): Promise<CodeImporters> {
  const { data } = await apiClient.get<CodeImporters>(
    `${repositoryPath(projectId, repositoryId)}/files/importers`,
    { params: { path } },
  )
  return data
}
