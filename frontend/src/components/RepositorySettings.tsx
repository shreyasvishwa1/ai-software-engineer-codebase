import { useRef, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'

import { useDeleteRepository, useUpdateRepository } from '../hooks/useRepository'
import { getErrorMessage } from '../services/client'
import type { Repository } from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const MAX_NAME_LENGTH = 255
const INPUT = 'mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2'
const BUTTON =
  'rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50'
const PRIMARY =
  'rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50'
const DANGER =
  'rounded-lg bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:cursor-not-allowed disabled:opacity-50'

type Mode = 'idle' | 'edit' | 'delete'

interface RepositorySettingsProps {
  /** The page's route ids: they key the page's cached queries and build the request URLs. */
  projectId: string
  repositoryId: string
  /** The server's repository: its name is shown, edited and confirmed; its id finds it in lists. */
  repository: Repository
}

interface FormProps extends RepositorySettingsProps {
  onClose: () => void
}

function EditRepository({ projectId, repositoryId, repository, onClose }: FormProps) {
  const update = useUpdateRepository(projectId, repositoryId)
  // The server name the form opened with; only a different name is sent.
  const [original] = useState(repository.name)
  const [name, setName] = useState(repository.name)
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
      setError('Repository name is required')
      return
    }
    if (trimmed.length > MAX_NAME_LENGTH) {
      setError(`Repository name must be at most ${MAX_NAME_LENGTH} characters`)
      return
    }
    setError(null)
    if (trimmed === original) {
      onClose()
      return
    }
    saving.current = true
    update.mutate(
      { name: trimmed },
      {
        onSuccess: onClose,
        onSettled: () => {
          saving.current = false
        },
      },
    )
  }

  const message =
    error ??
    (update.isError ? getErrorMessage(update.error, 'Could not update the repository') : null)
  return (
    <section aria-label="Edit repository details">
      <form onSubmit={onSubmit} className="space-y-3">
        <label className="block text-sm">
          <span className="text-slate-400">Repository name</span>
          <input className={INPUT} value={name} onChange={(e) => setName(e.target.value)} />
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

function DeleteRepository({ projectId, repositoryId, repository, onClose }: FormProps) {
  const remove = useDeleteRepository(projectId, repositoryId)
  const navigate = useNavigate()
  const [confirmation, setConfirmation] = useState('')
  const deleting = useRef(false)
  // Exact, case-sensitive and untrimmed: only the server's current name confirms.
  const confirmed = confirmation === repository.name

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (!confirmed || deleting.current) {
      return
    }
    deleting.current = true
    remove.mutate(repository.id, {
      // The hook has already removed the repository's cached data, so nothing refetches it.
      onSuccess: () => navigate(`/projects/${projectId}`, { replace: true }),
      onSettled: () => {
        deleting.current = false
      },
    })
  }

  return (
    <section
      aria-label="Confirm repository deletion"
      className="rounded-lg border border-red-500/30 bg-red-500/5 p-3"
    >
      <form onSubmit={onSubmit} className="space-y-3">
        <p className="text-sm text-red-200">
          This permanently deletes <strong className="font-semibold">{repository.name}</strong> with
          all of its files and analysis results. It cannot be undone.
        </p>
        <label className="block text-sm">
          <span className="text-slate-400">Type the repository name to confirm</span>
          <input
            className={INPUT}
            value={confirmation}
            onChange={(e) => setConfirmation(e.target.value)}
          />
        </label>
        <ErrorMessage
          message={
            remove.isError ? getErrorMessage(remove.error, 'Could not delete the repository') : null
          }
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
 * Rename or delete the displayed repository. The rename form opens from the server's name, sends
 * only a changed name and shows the server's response; deleting needs the exact repository name.
 * RepositoryPage keys this by repository id, so another repository always starts idle. All
 * repository text is rendered as plain text.
 */
export function RepositorySettings({ projectId, repositoryId, repository }: RepositorySettingsProps) {
  const [mode, setMode] = useState<Mode>('idle')
  const close = () => setMode('idle')
  const props = { projectId, repositoryId, repository, onClose: close }
  return (
    <section aria-label="Repository settings" className="mt-3 space-y-3">
      {mode === 'idle' && (
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={() => setMode('edit')} className={BUTTON}>
            Edit repository
          </button>
          <button type="button" onClick={() => setMode('delete')} className={BUTTON}>
            Delete repository
          </button>
        </div>
      )}
      {mode === 'edit' && <EditRepository {...props} />}
      {mode === 'delete' && <DeleteRepository {...props} />}
    </section>
  )
}
