import type { CodeSearchResult } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/**
 * Literal, case-insensitive search performed by the server. The query is sent exactly as
 * given, only in the JSON body (never in the URL).
 */
export async function searchCode(
  projectId: string,
  repositoryId: string,
  query: string,
): Promise<CodeSearchResult> {
  const { data } = await apiClient.post<CodeSearchResult>(
    `${repositoryPath(projectId, repositoryId)}/search`,
    { query },
  )
  return data
}
