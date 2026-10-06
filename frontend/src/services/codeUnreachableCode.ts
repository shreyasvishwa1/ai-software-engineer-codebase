import type { UnreachableCodeResult } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/**
 * Statements of one Python file that can never run (found by the server). The path is sent as a
 * query parameter (encoded by axios), never concatenated into the URL.
 */
export async function getCodeUnreachableCode(
  projectId: string,
  repositoryId: string,
  path: string,
): Promise<UnreachableCodeResult> {
  const { data } = await apiClient.get<UnreachableCodeResult>(
    `${repositoryPath(projectId, repositoryId)}/files/unreachable-code`,
    { params: { path } },
  )
  return data
}
