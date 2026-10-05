import type { CodeOccurrencesResult } from '../types/api'
import { apiClient } from './client'
import type { CodePosition } from './codeDefinition'
import { repositoryPath } from './repositories'

/**
 * Occurrences, in one Python file, of the binding of the name at one position, found by the
 * server. Only the path and the position are sent, as query parameters (encoded by axios).
 */
export async function getCodeOccurrences(
  projectId: string,
  repositoryId: string,
  path: string,
  position: CodePosition,
): Promise<CodeOccurrencesResult> {
  const { data } = await apiClient.get<CodeOccurrencesResult>(
    `${repositoryPath(projectId, repositoryId)}/files/occurrences`,
    { params: { path, line: position.line, column: position.column } },
  )
  return data
}
