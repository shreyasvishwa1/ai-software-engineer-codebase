import type { DependencyInventory } from '../types/api'
import { apiClient } from './client'
import { repositoryPath } from './repositories'

/** Dependencies declared in the repository's manifest files (computed by the server). */
export async function getDependencies(
  projectId: string,
  repositoryId: string,
): Promise<DependencyInventory> {
  const { data } = await apiClient.get<DependencyInventory>(
    `${repositoryPath(projectId, repositoryId)}/analysis/dependencies`,
  )
  return data
}
