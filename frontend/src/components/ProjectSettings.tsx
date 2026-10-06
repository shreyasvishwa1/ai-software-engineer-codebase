import { useRef, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'

import { useDeleteProject, useUpdateProject } from '../hooks/useProjects'
import { getErrorMessage } from '../services/client'
import type { Project, ProjectUpdate } from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const MAX_NAME_LENGTH = 100
const INPUT = 'mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2'
const BUTTON =
  'rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50'
const PRIMARY =
  'rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50'
const DANGER =
  'rounded-lg bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:cursor-not-allowed disabled:opacity-50'

type Mode = 'idle' | 'edit' | 'delete'

/** Only the fields whose normalized values differ from the server's project. */
function changedFields(original: Project, name: string, description: string | null): ProjectUpdate {
  const changes: ProjectUpdate = {}
  if (name !== original.name) {
    changes.name = name
  }
  if (description !== original.description) {
    changes.description = description
  }
  return changes
}

function EditProject({ project, onClose }: { project: Project; onClose: () => void }) {
  const update = useUpdateProject(project.id)
  // The server values the form opened with; the PATCH carries only what differs from them.
  const [original] = useState(project)
  const [name, setName] = useState(project.name)
  const [description, setDescription] = useState(project.description ?? '')
  const [error, setError] = useState<string | null>(null)
  // Set synchronously: the pending state only reaches the button on a later render.
  const saving = useRef(false)

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (saving.current) {
      return
    }
    const trimmed = name.trim()
    if (!trimmed) {
      setError('Project name is required')
      return
    }
    if (trimmed.length > MAX_NAME_LENGTH) {
      setError(`Project name must be at most ${MAX_NAME_LENGTH} characters`)
      return
    }
    setError(null)
    const changes = changedFields(original, trimmed, description.trim() || null)
    if (Object.keys(changes).length === 0) {
      onClose()
      return
    }
    saving.current = true
    update.mutate(changes, {
      onSuccess: onClose,
      onSettled: () => {
        saving.current = false
      },
    })
  }

  const message =
    error ?? (update.isError ? getErrorMessage(update.error, 'Could not update the project') : null)
  return (
    <section aria-label="Edit project details">
      <form onSubmit={onSubmit} className="space-y-3">
        <label className="block text-sm">
          <span className="text-slate-400">Project name</span>
          <input className={INPUT} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="block text-sm">
          <span className="text-slate-400">Project description (optional)</span>
          <textarea
            className={INPUT}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        <ErrorMessage message={message} />
        <div className="flex gap-2">
          <button type="submit" disabled={update.isPending} className={PRIMARY}>
            {update.isPending ? 'Saving…' : 'Save changes'}
          </button>
          <button type="button" onClick={onClose} disabled={update.isPending} className={BUTTON}>
            Cancel
          </button>
        </div>
      </form>
    </section>
  )
}

function DeleteProject({ project, onClose }: { project: Project; onClose: () => void }) {
  const remove = useDeleteProject(project.id)
  const navigate = useNavigate()
  const [confirmation, setConfirmation] = useState('')
  const deleting = useRef(false)
  // Exact, case-sensitive and untrimmed: only the server's current name confirms.
  const confirmed = confirmation === project.name

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (!confirmed || deleting.current) {
      return
    }
    deleting.current = true
    remove.mutate(undefined, {
      // The hook has already removed the project's cached data, so nothing refetches it.
      onSuccess: () => navigate('/projects', { replace: true }),
      onSettled: () => {
        deleting.current = false
      },
    })
  }

  return (
    <section
      aria-label="Confirm project deletion"
      className="rounded-lg border border-red-500/30 bg-red-500/5 p-3"
    >
      <form onSubmit={onSubmit} className="space-y-3">
        <p className="text-sm text-red-200">
          This permanently deletes <strong className="font-semibold">{project.name}</strong> with all
          of its repositories, files and analysis results. It cannot be undone.
        </p>
        <label className="block text-sm">
          <span className="text-slate-400">Type the project name to confirm</span>
          <input
            className={INPUT}
            value={confirmation}
            onChange={(e) => setConfirmation(e.target.value)}
          />
        </label>
        <ErrorMessage
          message={remove.isError ? getErrorMessage(remove.error, 'Could not delete the project') : null}
        />
        <div className="flex gap-2">
          <button type="submit" disabled={!confirmed || remove.isPending} className={DANGER}>
            {remove.isPending ? 'Deleting…' : 'Delete permanently'}
          </button>
          <button type="button" onClick={onClose} disabled={remove.isPending} className={BUTTON}>
            Cancel
          </button>
        </div>
      </form>
    </section>
  )
}

/**
 * Edit or delete the displayed project. The forms open from the server's project, a save sends
 * only the changed fields and shows the server's response, and deleting needs the exact project
 * name. ProjectPage keys this by project id, so another project always starts idle. All project
 * text is rendered as plain text.
 */
export function ProjectSettings({ project }: { project: Project }) {
  const [mode, setMode] = useState<Mode>('idle')
  const close = () => setMode('idle')
  return (
    <section aria-label="Project settings" className="mt-3 space-y-3">
      {mode === 'idle' && (
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={() => setMode('edit')} className={BUTTON}>
            Edit project
          </button>
          <button type="button" onClick={() => setMode('delete')} className={BUTTON}>
            Delete project
          </button>
        </div>
      )}
      {mode === 'edit' && <EditProject project={project} onClose={close} />}
      {mode === 'delete' && <DeleteProject project={project} onClose={close} />}
    </section>
  )
}
