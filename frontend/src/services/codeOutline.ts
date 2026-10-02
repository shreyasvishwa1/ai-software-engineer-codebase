import type { CodeOutline } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/**
 * Classes, functions and methods of one Python file (parsed by the server). The path is
 * sent as a query parameter (encoded by axios), never concatenated into the URL.
 */
export async function getCodeOutline(
  projectId: string,
  repositoryId: string,
  path: string,
): Promise<CodeOutline> {
  const { data } = await apiClient.get<CodeOutline>(
    `${repositoryPath(projectId, repositoryId)}/files/outline`,
    { params: { path } },
  )
  return data
}
