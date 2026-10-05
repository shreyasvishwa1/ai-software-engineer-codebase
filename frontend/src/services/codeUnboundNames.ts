import type { UnboundNamesResult } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/**
 * Identifier loads of one Python file with no statically known binding (found by the server).
 * The path is sent as a query parameter (encoded by axios), never concatenated into the URL.
 */
export async function getCodeUnboundNames(
  projectId: string,
  repositoryId: string,
  path: string,
): Promise<UnboundNamesResult> {
  const { data } = await apiClient.get<UnboundNamesResult>(
    `${repositoryPath(projectId, repositoryId)}/files/unbound-names`,
    { params: { path } },
  )
  return data
}
