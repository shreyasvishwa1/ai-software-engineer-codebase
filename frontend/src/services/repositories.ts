import type {
  IngestionJobResponse,
  Repository,
  RepositoryCreate,
  RepositoryLanguages,
  RepositoryTree,
  RepositoryUpdate,
} from '../types/api'
import { apiClient } from './client'

export function repositoriesPath(projectId: string): string {
  return `/projects/${encodeURIComponent(projectId)}/repositories`
}

export function repositoryPath(projectId: string, repositoryId: string): string {
  return `${repositoriesPath(projectId)}/${encodeURIComponent(repositoryId)}`
}

export async function listRepositories(projectId: string): Promise<Repository[]> {
  const { data } = await apiClient.get<Repository[]>(repositoriesPath(projectId))
  return data
}

export async function getRepository(projectId: string, repositoryId: string): Promise<Repository> {
  const { data } = await apiClient.get<Repository>(repositoryPath(projectId, repositoryId))
  return data
}

export async function createRepository(
  projectId: string,
  body: RepositoryCreate,
): Promise<Repository> {
  const { data } = await apiClient.post<Repository>(repositoriesPath(projectId), body)
  return data
}

export async function updateRepository(
  projectId: string,
  repositoryId: string,
  body: RepositoryUpdate,
): Promise<Repository> {
  const { data } = await apiClient.patch<Repository>(repositoryPath(projectId, repositoryId), body)
  return data
}

/** Deletes the repository with all of its files and analysis results (204). */
export async function deleteRepository(projectId: string, repositoryId: string): Promise<void> {
  await apiClient.delete(repositoryPath(projectId, repositoryId))
}

export async function getRepositoryTree(
  projectId: string,
  repositoryId: string,
): Promise<RepositoryTree> {
  const { data } = await apiClient.get<RepositoryTree>(
    `${repositoryPath(projectId, repositoryId)}/tree`,
  )
  return data
}

export async function getRepositoryLanguages(
  projectId: string,
  repositoryId: string,
): Promise<RepositoryLanguages> {
  const { data } = await apiClient.get<RepositoryLanguages>(
    `${repositoryPath(projectId, repositoryId)}/languages`,
  )
  return data
}

/** Upload a ZIP archive (multipart/form-data, field "file"); returns the queued job. */
export async function ingestZip(
  projectId: string,
  repositoryId: string,
  file: File,
  onUploadProgress?: (percent: number) => void,
): Promise<IngestionJobResponse> {
  const form = new FormData()
  form.append('file', file)
  const { data } = await apiClient.post<IngestionJobResponse>(
    `${repositoryPath(projectId, repositoryId)}/ingest`,
    form,
    {
      timeout: 0, // large uploads; the server enforces its own limits
      onUploadProgress: (event) => {
        if (onUploadProgress && event.total) {
          onUploadProgress(Math.round((event.loaded * 100) / event.total))
        }
      },
    },
  )
  return data
}

/** Queue GitHub ingestion: no request body; the server uses the repository's source_url. */
export async function ingestGithub(
  projectId: string,
  repositoryId: string,
): Promise<IngestionJobResponse> {
  const { data } = await apiClient.post<IngestionJobResponse>(
    `${repositoryPath(projectId, repositoryId)}/ingest`,
  )
  return data
}
