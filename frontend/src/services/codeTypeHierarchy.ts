import type { TypeHierarchyResult } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/**
 * Direct bases and direct subclasses of the class statement starting on ``line`` of
 * ``path``, found by the server. Only the path and the line are sent, as query parameters
 * (encoded by axios).
 */
export async function getCodeTypeHierarchy(
  projectId: string,
  repositoryId: string,
  path: string,
  line: number,
): Promise<TypeHierarchyResult> {
  const { data } = await apiClient.get<TypeHierarchyResult>(
    `${repositoryPath(projectId, repositoryId)}/files/type-hierarchy`,
    { params: { path, line } },
  )
  return data
}
