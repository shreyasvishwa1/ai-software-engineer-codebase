// Types mirroring the backend's /api/v1 response schemas.

export type RepositoryStatus = 'pending' | 'processing' | 'ready' | 'failed'
export type RepositorySourceType = 'zip' | 'github'
export type AnalysisJobStatus = 'pending' | 'processing' | 'completed' | 'failed'
export type AnalysisJobType = 'ingestion' | 'code_metrics'

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
  job_type: AnalysisJobType
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

export interface FileContent {
  path: string
  language: string | null
  size_bytes: number
  /** null when the file cannot be shown as text; see reason */
  content: string | null
  reason: 'binary' | 'too_large' | null
}

export type FileMetricStatus = 'analyzed' | 'binary' | 'too_large' | 'unavailable'

export interface FileMetric {
  path: string
  language: string | null
  status: FileMetricStatus
  /** set only for analyzed files */
  line_count: number | null
  blank_line_count: number | null
}

export interface CodeMetricsSummary {
  files: number
  analyzed: number
  binary: number
  too_large: number
  unavailable: number
  total_lines: number
  blank_lines: number
}

export interface CodeMetrics {
  /** the latest code-metrics job, if any */
  job: AnalysisJob | null
  /** null until the current snapshot is analyzed */
  summary: CodeMetricsSummary | null
  files: FileMetric[]
}

export interface CodeMetricsJob {
  job_id: string
  repository_id: string
  status: AnalysisJobStatus
}

export interface FileExplanation {
  path: string
  /** plain text from the model; untrusted, render as text only */
  explanation: string
  model: string
  input_tokens: number | null
  output_tokens: number | null
}

export type ManifestStatus = 'parsed' | 'invalid' | 'too_large' | 'unavailable'

export interface DependencyEntry {
  name: string
  /** extras, version constraints and markers exactly as written */
  specifier: string
  /** runtime | dev | peer | optional | optional:<group> */
  scope: string
}

export interface DependencyManifest {
  path: string
  ecosystem: 'npm' | 'python'
  status: ManifestStatus
  dependencies: DependencyEntry[]
}

export interface DependencySummary {
  manifests: number
  parsed: number
  invalid: number
  too_large: number
  unavailable: number
  dependencies: number
}

export interface DependencyInventory {
  manifests: DependencyManifest[]
  summary: DependencySummary
  truncated: boolean
}

export type SecretRule =
  | 'aws-access-key-id'
  | 'github-token'
  | 'slack-token'
  | 'stripe-live-key'
  | 'google-api-key'
  | 'anthropic-api-key'
  | 'private-key'

/** Location of a likely secret; the server never sends the matched value. */
export interface SecretFinding {
  /** POSIX path relative to the repository root */
  path: string
  /** 1-based */
  line: number
  rule: SecretRule
  description: string
}

export interface SecretScanSummary {
  files_scanned: number
  /** includes content that is not valid UTF-8 */
  binary: number
  too_large: number
  unavailable: number
  findings: number
}

export interface SecretScan {
  findings: SecretFinding[]
  summary: SecretScanSummary
  truncated: boolean
}

/** One matching line from the server's literal, case-insensitive search. */
export interface CodeSearchMatch {
  /** POSIX path relative to the repository root */
  path: string
  /** 1-based */
  line: number
  /** at most 200 characters of the matching line; untrusted, render as text only */
  preview: string
}

export interface CodeSearchSummary {
  files_scanned: number
  /** includes content that is not valid UTF-8 */
  binary: number
  too_large: number
  unavailable: number
  matches: number
}

export interface CodeSearchResult {
  matches: CodeSearchMatch[]
  summary: CodeSearchSummary
  truncated: boolean
}

export type OutlineStatus =
  | 'parsed'
  | 'syntax_error'
  | 'too_large'
  | 'binary'
  | 'unsupported_language'

/** One class, function or method of a Python file; the name is untrusted text. */
export interface OutlineSymbol {
  kind: 'class' | 'function' | 'method'
  async: boolean
  name: string
  /** 1-based line of the class/def statement */
  line: number
  end_line: number
  /** 0 = top level */
  depth: number
}

export interface CodeOutline {
  path: string
  status: OutlineStatus
  /** empty unless status is 'parsed' */
  symbols: OutlineSymbol[]
  truncated: boolean
}

/** One import statement of a Python file, as resolved by the server (authoritative). */
export interface ImportEntry {
  line: number
  statement: 'import' | 'from'
  /** "from" module as written; null for "import" and "from . import" */
  module: string | null
  /** leading dots of a relative import; 0 = absolute */
  level: number
  names: string[]
  /** matching repository paths, sorted by the server (at most 5) */
  targets: string[]
  resolution: 'resolved' | 'ambiguous' | 'not_found'
}

export interface CodeImports {
  path: string
  status: OutlineStatus
  /** empty unless status is 'parsed' */
  imports: ImportEntry[]
  truncated: boolean
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
