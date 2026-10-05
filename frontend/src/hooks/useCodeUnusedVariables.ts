import { useMutation } from '@tanstack/react-query'

import { getCodeUnusedVariables } from '../services/codeUnusedVariables'

/** Find a file's unused local variables on demand only; results are never cached or stored. */
export function useCodeUnusedVariables(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: (path: string) => getCodeUnusedVariables(projectId, repositoryId, path),
  })
}
