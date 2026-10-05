import { useMutation } from '@tanstack/react-query'

import { getCodeUnboundNames } from '../services/codeUnboundNames'

/** Find a file's statically unbound names on demand only; results are never cached or stored. */
export function useCodeUnboundNames(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: (path: string) => getCodeUnboundNames(projectId, repositoryId, path),
  })
}
