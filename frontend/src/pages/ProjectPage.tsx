import { GitBranch } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'

import { CreateRepositoryForm } from '../components/CreateRepositoryForm'
import { ErrorMessage } from '../components/ErrorMessage'
import { ProjectSettings } from '../components/ProjectSettings'
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
      <div className="relative overflow-hidden rounded-3xl border border-slate-800 bg-gradient-to-br from-violet-600/25 via-indigo-600/15 to-cyan-500/10 p-8 before:pointer-events-none before:absolute before:-right-12 before:-top-12 before:h-48 before:w-48 before:rounded-full before:bg-cyan-400/15 before:blur-3xl before:content-['']">
        <Link to="/projects" className="text-sm text-indigo-300 hover:underline">
          ← Projects
        </Link>
        <h1 className="mt-3 text-3xl font-extrabold tracking-tight">{project.data.name}</h1>
        {project.data.description && (
          <p className="mt-2 max-w-2xl text-slate-300">{project.data.description}</p>
        )}
        {/* Keyed by id: another project never shows this project's forms or errors. */}
        <ProjectSettings key={project.data.id} project={project.data} />
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
            <p className="rounded-2xl border border-dashed border-slate-700 p-6 text-center text-sm text-slate-400">
              No repositories yet.
            </p>
          )}
          <ul className="space-y-2">
            {repositories.data?.map((repository) => (
              <li key={repository.id}>
                <Link
                  to={`/projects/${projectId}/repositories/${repository.id}`}
                  className="group flex items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-900/70 p-4 transition hover:border-indigo-500/50 hover:shadow-glow"
                >
                  <span className="flex min-w-0 items-center gap-3">
                    <span
                      aria-hidden="true"
                      className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-cyan-500 to-indigo-500"
                    >
                      <GitBranch className="h-4 w-4 text-white" />
                    </span>
                    <span className="min-w-0">
                      <span className="font-semibold group-hover:text-indigo-200">{repository.name}</span>
                      <span className="ml-2 rounded-md bg-slate-800 px-1.5 py-0.5 text-xs text-slate-400">
                        {repository.source_type}
                      </span>
                    </span>
                  </span>
                  <StatusBadge status={repository.status} />
                </Link>
              </li>
            ))}
          </ul>
        </section>
        <aside className="glass-card h-fit p-5">
          <CreateRepositoryForm projectId={projectId} />
        </aside>
      </div>
    </div>
  )
}
