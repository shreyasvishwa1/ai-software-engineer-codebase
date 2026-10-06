import { useMutation } from '@tanstack/react-query'

import { getCodeUnreachableCode } from '../services/codeUnreachableCode'

/** Find a file's unreachable code on demand only; results are never cached or stored. */
export function useCodeUnreachableCode(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: (path: string) => getCodeUnreachableCode(projectId, repositoryId, path),
  })
}
