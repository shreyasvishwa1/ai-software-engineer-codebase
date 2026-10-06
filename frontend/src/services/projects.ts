import type { Project, ProjectCreate, ProjectUpdate } from '../types/api'
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

export async function updateProject(projectId: string, body: ProjectUpdate): Promise<Project> {
  const { data } = await apiClient.patch<Project>(
    `/projects/${encodeURIComponent(projectId)}`,
    body,
  )
  return data
}

/** Deletes the project with all of its repositories, files and analysis results (204). */
export async function deleteProject(projectId: string): Promise<void> {
  await apiClient.delete(`/projects/${encodeURIComponent(projectId)}`)
}
