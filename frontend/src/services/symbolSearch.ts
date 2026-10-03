import type { SymbolSearchResult } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/**
 * Python definitions whose name equals the query (matched by the server). The query is sent
 * exactly as given, only in the JSON body (never in the URL).
 */
export async function searchSymbols(
  projectId: string,
  repositoryId: string,
  query: string,
): Promise<SymbolSearchResult> {
  const { data } = await apiClient.post<SymbolSearchResult>(
    `${repositoryPath(projectId, repositoryId)}/symbols/search`,
    { query },
  )
  return data
}
