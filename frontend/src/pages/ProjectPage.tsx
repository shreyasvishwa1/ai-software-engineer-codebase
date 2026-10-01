import { Link, useParams } from 'react-router-dom'

import { CreateRepositoryForm } from '../components/CreateRepositoryForm'
import { ErrorMessage } from '../components/ErrorMessage'
import { StatusBadge } from '../components/StatusBadge'
import { useProject } from '../hooks/useProjects'
import { useRepositories } from '../hooks/useRepository'
import { getErrorMessage } from '../services/client'

export function ProjectPage() {
  const { projectId = '' } = useParams()
  const project = useProject(projectId)
  const repositories = useRepositories(projectId)

  if (project.isPending) {
    return <p className="text-sm text-slate-400">Loading project…</p>
  }
  if (project.isError) {
    return <ErrorMessage message={getErrorMessage(project.error, 'Could not load the project')} />
  }

  return (
    <div className="space-y-6">
      <div>
        <Link to="/projects" className="text-sm text-indigo-300 hover:underline">
          ← Projects
        </Link>
        <h1 className="mt-2 text-2xl font-bold">{project.data.name}</h1>
        {project.data.description && (
          <p className="mt-1 text-slate-400">{project.data.description}</p>
        )}
      </div>
      <div className="grid gap-8 md:grid-cols-[2fr_1fr]">
        <section className="space-y-3">
          <h2 className="text-lg font-semibold">Repositories</h2>
          {repositories.isPending && <p className="text-sm text-slate-400">Loading repositories…</p>}
          {repositories.isError && (
            <ErrorMessage
              message={getErrorMessage(repositories.error, 'Could not load repositories')}
            />
          )}
          {repositories.data?.length === 0 && (
            <p className="text-sm text-slate-400">No repositories yet.</p>
          )}
          <ul className="space-y-2">
            {repositories.data?.map((repository) => (
              <li key={repository.id}>
                <Link
                  to={`/projects/${projectId}/repositories/${repository.id}`}
                  className="flex items-center justify-between rounded-xl border border-slate-800 bg-slate-900 p-4 hover:border-slate-600"
                >
                  <span>
                    <span className="font-medium">{repository.name}</span>
                    <span className="ml-2 text-xs text-slate-500">{repository.source_type}</span>
                  </span>
                  <StatusBadge status={repository.status} />
                </Link>
              </li>
            ))}
          </ul>
        </section>
        <aside className="rounded-xl border border-slate-800 bg-slate-900 p-4">
          <CreateRepositoryForm projectId={projectId} />
        </aside>
      </div>
    </div>
  )
}
