import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  createRepository,
  getRepository,
  getRepositoryLanguages,
  getRepositoryTree,
  ingestGithub,
  ingestZip,
  listRepositories,
} from '../services/repositories'
import type { RepositoryCreate } from '../types/api'
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
