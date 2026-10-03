import type { CodeReferencesResult } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/**
 * References to the class/def statement starting on ``line`` of ``path``, found by the
 * server. Only the path and the line are sent, as query parameters (encoded by axios).
 */
export async function getCodeReferences(
  projectId: string,
  repositoryId: string,
  path: string,
  line: number,
): Promise<CodeReferencesResult> {
  const { data } = await apiClient.get<CodeReferencesResult>(
    `${repositoryPath(projectId, repositoryId)}/files/references`,
    { params: { path, line } },
  )
  return data
}
