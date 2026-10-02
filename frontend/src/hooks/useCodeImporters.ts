import { useMutation } from '@tanstack/react-query'

import { getCodeImporters } from '../services/codeImporters'

/** Load a file's importers on demand only; results are never cached or stored. */
export function useCodeImporters(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: (path: string) => getCodeImporters(projectId, repositoryId, path),
  })
}
