import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { createProject, getProject, listProjects } from '../services/projects'
import type { ProjectCreate } from '../types/api'
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
