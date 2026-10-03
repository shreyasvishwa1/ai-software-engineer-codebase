import { useMutation } from '@tanstack/react-query'

import { type CodePosition, getCodeDefinition } from '../services/codeDefinition'

/** Resolve a definition on demand only; results are never cached or stored. */
export function useCodeDefinition(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: ({ path, position }: { path: string; position: CodePosition }) =>
      getCodeDefinition(projectId, repositoryId, path, position),
  })
}
