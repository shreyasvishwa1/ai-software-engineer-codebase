import { useMutation } from '@tanstack/react-query'

import { getCodeOutline } from '../services/codeOutline'

/** Load a file's outline on demand only; results are never cached or stored. */
export function useCodeOutline(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: (path: string) => getCodeOutline(projectId, repositoryId, path),
  })
}
