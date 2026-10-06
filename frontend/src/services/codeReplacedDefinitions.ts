import type { ReplacedDefinitionsResult } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/**
 * Def and class statements of one Python file replaced by a later one of the same name before
 * anything read them (found by the server). The path is sent as a query parameter (encoded by
 * axios), never concatenated into the URL.
 */
export async function getCodeReplacedDefinitions(
  projectId: string,
  repositoryId: string,
  path: string,
): Promise<ReplacedDefinitionsResult> {
  const { data } = await apiClient.get<ReplacedDefinitionsResult>(
    `${repositoryPath(projectId, repositoryId)}/files/replaced-definitions`,
    { params: { path } },
  )
  return data
}
