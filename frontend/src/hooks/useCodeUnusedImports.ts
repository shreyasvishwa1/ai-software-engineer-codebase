import { useMutation } from '@tanstack/react-query'

import { getCodeUnusedImports } from '../services/codeUnusedImports'

/** Find a file's unused imports on demand only; results are never cached or stored. */
export function useCodeUnusedImports(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: (path: string) => getCodeUnusedImports(projectId, repositoryId, path),
  })
}
