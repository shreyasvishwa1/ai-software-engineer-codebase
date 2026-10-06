import { useMutation } from '@tanstack/react-query'

import { getCodeReplacedDefinitions } from '../services/codeReplacedDefinitions'

/** Find a file's replaced definitions on demand only; results are never cached or stored. */
export function useCodeReplacedDefinitions(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: (path: string) => getCodeReplacedDefinitions(projectId, repositoryId, path),
  })
}
