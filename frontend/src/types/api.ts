// Types mirroring the backend's /api/v1 response schemas.

export type RepositoryStatus = 'pending' | 'processing' | 'ready' | 'failed'
export type RepositorySourceType = 'zip' | 'github'
export type AnalysisJobStatus = 'pending' | 'processing' | 'completed' | 'failed'

export interface User {
  id: string
  email: string
  created_at: string
  updated_at: string
}

export interface TokenResponse {
  access_token: string
  token_type: 'bearer'
}

export interface Project {
  id: string
  name: string
  description: string | null
  created_at: string
  updated_at: string
}

export interface ProjectCreate {
  name: string
  description?: string | null
}

export interface Repository {
  id: string
  project_id: string
  name: string
  source_type: RepositorySourceType
  source_url: string | null
  default_branch: string | null
  status: RepositoryStatus
  created_at: string
  updated_at: string
}

export interface RepositoryCreate {
  name: string
  source_type: RepositorySourceType
  source_url?: string
  default_branch?: string
}

export interface AnalysisJob {
  id: string
  project_id: string
  repository_id: string
  job_type: 'ingestion'
  status: AnalysisJobStatus
  progress: number
  error_message: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export interface IngestionJobResponse {
  job_id: string
  repository_id: string
  status: AnalysisJobStatus
}

export interface TreeFile {
  type: 'file'
  name: string
  path: string
  extension: string | null
  language: string | null
  size_bytes: number
}

export interface TreeDirectory {
  type: 'directory'
  name: string
  path: string
  children: TreeNode[]
}

export type TreeNode = TreeDirectory | TreeFile

export interface RepositoryTree {
  repository_id: string
  status: RepositoryStatus
  file_count: number
  total_bytes: number
  tree: TreeDirectory
}

export interface LanguageStatistic {
  language: string | null
  file_count: number
  total_bytes: number
  percentage: number
}

export interface RepositoryLanguages {
  repository_id: string
  status: RepositoryStatus
  total_files: number
  total_bytes: number
  languages: LanguageStatistic[]
}

export interface Health {
  status: string
}
