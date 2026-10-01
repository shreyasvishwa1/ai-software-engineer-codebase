import type { Project, ProjectCreate } from '../types/api'
import { apiClient } from './client'

export async function listProjects(): Promise<Project[]> {
  const { data } = await apiClient.get<Project[]>('/projects')
  return data
}

export async function getProject(projectId: string): Promise<Project> {
  const { data } = await apiClient.get<Project>(`/projects/${encodeURIComponent(projectId)}`)
  return data
}

export async function createProject(body: ProjectCreate): Promise<Project> {
  const { data } = await apiClient.post<Project>('/projects', body)
  return data
}
