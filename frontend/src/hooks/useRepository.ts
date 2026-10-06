import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  createRepository,
  deleteRepository,
  getRepository,
  getRepositoryLanguages,
  getRepositoryTree,
  ingestGithub,
  ingestZip,
  listRepositories,
  updateRepository,
} from '../services/repositories'
import type { Repository, RepositoryCreate, RepositoryUpdate } from '../types/api'
import { queryKeys } from './queryKeys'

export function useRepositories(projectId: string) {
  return useQuery({
    queryKey: queryKeys.repositories(projectId),
    queryFn: () => listRepositories(projectId),
  })
}

export function useRepository(projectId: string, repositoryId: string) {
  return useQuery({
    queryKey: queryKeys.repository(projectId, repositoryId),
    queryFn: () => getRepository(projectId, repositoryId),
  })
}

export function useCreateRepository(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: RepositoryCreate) => createRepository(projectId, body),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.repositories(projectId) }),
  })
}

/**
 * Renames a repository: only the name is ever sent. The server's repository replaces the cached
 * repository and its entry in the project's list.
 */
export function useUpdateRepository(projectId: string, repositoryId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ name }: RepositoryUpdate) => updateRepository(projectId, repositoryId, { name }),
    onSuccess: (repository) => {
      queryClient.setQueryData(queryKeys.repository(projectId, repositoryId), repository)
      queryClient.setQueryData<Repository[]>(queryKeys.repositories(projectId), (repositories) =>
        repositories?.map((cached) => (cached.id === repository.id ? repository : cached)),
      )
    },
  })
}

/**
 * Runs before the caller navigates away: the repository leaves the project's cached list (found by
 * its server id, the mutation variable), then every cached query of it, all keyed under
 * queryKeys.repository, is removed, so none can refetch it.
 */
export function useDeleteRepository(projectId: string, repositoryId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (_id: string) => deleteRepository(projectId, repositoryId),
    onSuccess: (_data, id) => {
      queryClient.setQueryData<Repository[]>(queryKeys.repositories(projectId), (repositories) =>
        repositories?.filter((cached) => cached.id !== id),
      )
      queryClient.removeQueries({ queryKey: queryKeys.repository(projectId, repositoryId) })
    },
  })
}

export function useRepositoryTree(projectId: string, repositoryId: string) {
  return useQuery({
    queryKey: queryKeys.tree(projectId, repositoryId),
    queryFn: () => getRepositoryTree(projectId, repositoryId),
  })
}

export function useRepositoryLanguages(projectId: string, repositoryId: string) {
  return useQuery({
    queryKey: queryKeys.languages(projectId, repositoryId),
    queryFn: () => getRepositoryLanguages(projectId, repositoryId),
  })
}

/** Start ingestion (ZIP upload or GitHub); on success the job list is refreshed (polling). */
export function useIngest(projectId: string, repositoryId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ file, onProgress }: { file?: File; onProgress?: (percent: number) => void }) =>
      file
        ? ingestZip(projectId, repositoryId, file, onProgress)
        : ingestGithub(projectId, repositoryId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.jobs(projectId, repositoryId) })
      void queryClient.invalidateQueries({
        queryKey: queryKeys.repository(projectId, repositoryId),
        exact: true,
      })
    },
  })
}
