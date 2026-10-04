import type { CallHierarchyResult } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/**
 * Callers and callees of the class/def statement starting on ``line`` of ``path``, found by
 * the server. Only the path and the line are sent, as query parameters (encoded by axios).
 */
export async function getCodeCallHierarchy(
  projectId: string,
  repositoryId: string,
  path: string,
  line: number,
): Promise<CallHierarchyResult> {
  const { data } = await apiClient.get<CallHierarchyResult>(
    `${repositoryPath(projectId, repositoryId)}/files/call-hierarchy`,
    { params: { path, line } },
  )
  return data
}
