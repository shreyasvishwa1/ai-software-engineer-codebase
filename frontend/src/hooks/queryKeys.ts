export const queryKeys = {
  health: ['health'] as const,
  projects: ['projects'] as const,
  project: (projectId: string) => ['projects', projectId] as const,
  repositories: (projectId: string) => ['projects', projectId, 'repositories'] as const,
  repository: (projectId: string, repositoryId: string) =>
    ['projects', projectId, 'repositories', repositoryId] as const,
  tree: (projectId: string, repositoryId: string) =>
    ['projects', projectId, 'repositories', repositoryId, 'tree'] as const,
  languages: (projectId: string, repositoryId: string) =>
    ['projects', projectId, 'repositories', repositoryId, 'languages'] as const,
  jobs: (projectId: string, repositoryId: string) =>
    ['projects', projectId, 'repositories', repositoryId, 'jobs'] as const,
  fileContent: (projectId: string, repositoryId: string, path: string) =>
    ['projects', projectId, 'repositories', repositoryId, 'file-content', path] as const,
  codeMetrics: (projectId: string, repositoryId: string) =>
    ['projects', projectId, 'repositories', repositoryId, 'code-metrics'] as const,
  dependencies: (projectId: string, repositoryId: string) =>
    ['projects', projectId, 'repositories', repositoryId, 'dependencies'] as const,
}
