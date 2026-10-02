import type { SecretScan } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/** Locations of likely committed credentials (scanned by the server; never the values). */
export async function getSecretScan(projectId: string, repositoryId: string): Promise<SecretScan> {
  const { data } = await apiClient.get<SecretScan>(
    `${repositoryPath(projectId, repositoryId)}/analysis/secrets`,
  )
  return data
}
