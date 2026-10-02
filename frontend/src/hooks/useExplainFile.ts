import { useMutation } from '@tanstack/react-query'

import { explainFile } from '../services/fileExplanation'

/** Explain a file on demand only; results are never cached or stored. */
export function useExplainFile(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: (path: string) => explainFile(projectId, repositoryId, path),
  })
}
