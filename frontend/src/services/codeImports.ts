import type { CodeImports } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/**
 * Imports of one Python file, resolved to repository files by the server. The path is sent
 * as a query parameter (encoded by axios), never concatenated into the URL.
 */
export async function getCodeImports(
  projectId: string,
  repositoryId: string,
  path: string,
): Promise<CodeImports> {
  const { data } = await apiClient.get<CodeImports>(
    `${repositoryPath(projectId, repositoryId)}/files/imports`,
    { params: { path } },
  )
  return data
}
