import { Link } from 'react-router-dom'

import { CreateProjectForm } from '../components/CreateProjectForm'
import { ErrorMessage } from '../components/ErrorMessage'
import { useProjects } from '../hooks/useProjects'
import { getErrorMessage } from '../services/client'

export function ProjectsPage() {
  const projects = useProjects()
  return (
    <div className="grid gap-8 md:grid-cols-[2fr_1fr]">
      <section className="space-y-3">
        <h1 className="text-2xl font-bold">Projects</h1>
        {projects.isPending && <p className="text-sm text-slate-400">Loading projects…</p>}
        {projects.isError && (
          <ErrorMessage message={getErrorMessage(projects.error, 'Could not load projects')} />
        )}
        {projects.data?.length === 0 && (
          <p className="text-sm text-slate-400">No projects yet. Create your first one.</p>
        )}
        <ul className="space-y-2">
          {projects.data?.map((project) => (
            <li key={project.id}>
              <Link
                to={`/projects/${project.id}`}
                className="block rounded-xl border border-slate-800 bg-slate-900 p-4 hover:border-slate-600"
              >
                <span className="font-medium">{project.name}</span>
                {project.description && (
                  <span className="mt-1 block text-sm text-slate-400">{project.description}</span>
                )}
              </Link>
            </li>
          ))}
        </ul>
      </section>
      <aside className="rounded-xl border border-slate-800 bg-slate-900 p-4">
        <CreateProjectForm />
      </aside>
    </div>
  )
}
