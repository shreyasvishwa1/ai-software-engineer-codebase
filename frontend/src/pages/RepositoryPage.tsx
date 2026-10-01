import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { ErrorMessage } from '../components/ErrorMessage'
import { FileTree } from '../components/FileTree'
import { FileViewer } from '../components/FileViewer'
import { IngestPanel } from '../components/IngestPanel'
import { JobProgress } from '../components/JobProgress'
import { LanguageStats } from '../components/LanguageStats'
import { StatusBadge } from '../components/StatusBadge'
import {
  useRepository,
  useRepositoryLanguages,
  useRepositoryTree,
} from '../hooks/useRepository'
import { getErrorMessage } from '../services/client'
import { formatBytes } from '../utils/format'

const CARD = 'rounded-xl border border-slate-800 bg-slate-900 p-4'

export function RepositoryPage() {
  const { projectId = '', repositoryId = '' } = useParams()
  const repository = useRepository(projectId, repositoryId)
  const tree = useRepositoryTree(projectId, repositoryId)
  const languages = useRepositoryLanguages(projectId, repositoryId)
  const [selectedPath, setSelectedPath] = useState<string | null>(null)

  if (repository.isPending) {
    return <p className="text-sm text-slate-400">Loading repository…</p>
  }
  if (repository.isError) {
    return (
      <ErrorMessage message={getErrorMessage(repository.error, 'Could not load the repository')} />
    )
  }
  const repo = repository.data

  return (
    <div className="space-y-6">
      <div>
        <Link to={`/projects/${projectId}`} className="text-sm text-indigo-300 hover:underline">
          ← Project
        </Link>
        <div className="mt-2 flex items-center gap-3">
          <h1 className="text-2xl font-bold">{repo.name}</h1>
          <StatusBadge status={repo.status} />
        </div>
        <p className="mt-1 text-sm text-slate-400">
          Source: {repo.source_type}
          {/* Shown as plain text on purpose: never a clickable external link. */}
          {repo.source_url && <span className="ml-2 break-all font-mono">{repo.source_url}</span>}
          {repo.default_branch && <span className="ml-2">({repo.default_branch})</span>}
        </p>
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        <div className={CARD}>
          <IngestPanel repository={repo} />
        </div>
        <div className={CARD}>
          <JobProgress projectId={projectId} repositoryId={repositoryId} />
        </div>
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        <section className={CARD} aria-label="Files">
          <h2 className="mb-3 text-sm font-semibold text-slate-200">
            Files
            {tree.data && (
              <span className="ml-2 font-normal text-slate-500">
                {tree.data.file_count} files · {formatBytes(tree.data.total_bytes)}
              </span>
            )}
          </h2>
          {tree.isPending && <p className="text-sm text-slate-400">Loading files…</p>}
          {tree.isError && (
            <ErrorMessage message={getErrorMessage(tree.error, 'Could not load files')} />
          )}
          {tree.data && (
            <FileTree
              tree={tree.data.tree}
              onSelectFile={setSelectedPath}
              selectedPath={selectedPath}
            />
          )}
        </section>
        <section className={CARD} aria-label="Languages">
          <h2 className="mb-3 text-sm font-semibold text-slate-200">Languages</h2>
          {languages.isPending && <p className="text-sm text-slate-400">Loading languages…</p>}
          {languages.isError && (
            <ErrorMessage
              message={getErrorMessage(languages.error, 'Could not load language statistics')}
            />
          )}
          {languages.data && <LanguageStats languages={languages.data.languages} />}
        </section>
      </div>

      <section className={CARD} aria-label="File">
        <h2 className="mb-3 text-sm font-semibold text-slate-200">File</h2>
        <FileViewer projectId={projectId} repositoryId={repositoryId} selectedPath={selectedPath} />
      </section>
    </div>
  )
}
