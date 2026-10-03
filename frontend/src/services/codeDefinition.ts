import type { CodeDefinitionResult } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/** A 1-based line and 1-based Unicode code-point column in a file. */
export interface CodePosition {
  line: number
  column: number
}

/**
 * Definition of the identifier at one position of a Python file, resolved by the server.
 * Only the path and the position are sent, as query parameters (encoded by axios).
 */
export async function getCodeDefinition(
  projectId: string,
  repositoryId: string,
  path: string,
  position: CodePosition,
): Promise<CodeDefinitionResult> {
  const { data } = await apiClient.get<CodeDefinitionResult>(
    `${repositoryPath(projectId, repositoryId)}/files/definition`,
    { params: { path, line: position.line, column: position.column } },
  )
  return data
}
