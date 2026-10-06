import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  createProject,
  deleteProject,
  getProject,
  listProjects,
  updateProject,
} from '../services/projects'
import type { Project, ProjectCreate, ProjectUpdate } from '../types/api'
import { queryKeys } from './queryKeys'

export function useProjects() {
  return useQuery({ queryKey: queryKeys.projects, queryFn: listProjects })
}

export function useProject(projectId: string) {
  return useQuery({ queryKey: queryKeys.project(projectId), queryFn: () => getProject(projectId) })
}

export function useCreateProject() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: ProjectCreate) => createProject(body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projects }),
  })
}

/** The server's project replaces the cached project and its entry in the cached list. */
export function useUpdateProject(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: ProjectUpdate) => updateProject(projectId, body),
    onSuccess: (project) => {
      queryClient.setQueryData(queryKeys.project(projectId), project)
      queryClient.setQueryData<Project[]>(queryKeys.projects, (projects) =>
        projects?.map((cached) => (cached.id === projectId ? project : cached)),
      )
    },
  })
}

/**
 * Runs before the caller navigates away: the project leaves the cached list and every cached
 * query of it (all keyed under queryKeys.project) is removed, so none can refetch it.
 */
export function useDeleteProject(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => deleteProject(projectId),
    onSuccess: () => {
      queryClient.setQueryData<Project[]>(queryKeys.projects, (projects) =>
        projects?.filter((cached) => cached.id !== projectId),
      )
      queryClient.removeQueries({ queryKey: queryKeys.project(projectId) })
    },
  })
}
