import type { FileExplanation } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/**
 * Ask the server to explain one repository file (Claude, server-side). The path is sent as
 * a query parameter (encoded by axios); there is no request body.
 */
export async function explainFile(
  projectId: string,
  repositoryId: string,
  path: string,
): Promise<FileExplanation> {
  const { data } = await apiClient.post<FileExplanation>(
    `${repositoryPath(projectId, repositoryId)}/files/explain`,
    undefined,
    { params: { path } },
  )
  return data
}
