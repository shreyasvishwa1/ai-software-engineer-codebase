import type { FileContent } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/**
 * The content of one repository file. The path is sent as a query parameter (encoded by
 * axios), never concatenated into the URL.
 */
export async function getFileContent(
  projectId: string,
  repositoryId: string,
  path: string,
): Promise<FileContent> {
  const { data } = await apiClient.get<FileContent>(
    `${repositoryPath(projectId, repositoryId)}/files/content`,
    { params: { path } },
  )
  return data
}
