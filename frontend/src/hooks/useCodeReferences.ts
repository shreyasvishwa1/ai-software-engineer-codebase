import { useMutation } from '@tanstack/react-query'

import { getCodeReferences } from '../services/codeReferences'
import type { DefinitionLocation } from '../types/api'

/** Find a definition's references on demand only; results are never cached or stored. */
export function useCodeReferences(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: (definition: DefinitionLocation) =>
      getCodeReferences(projectId, repositoryId, definition.path, definition.line),
  })
}
