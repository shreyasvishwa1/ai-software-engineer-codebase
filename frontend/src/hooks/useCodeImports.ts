import { useMutation } from '@tanstack/react-query'

import { getCodeImports } from '../services/codeImports'

/** Load a file's resolved imports on demand only; results are never cached or stored. */
export function useCodeImports(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: (path: string) => getCodeImports(projectId, repositoryId, path),
  })
}
