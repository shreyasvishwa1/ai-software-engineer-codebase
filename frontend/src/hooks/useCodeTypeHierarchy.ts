import { useMutation } from '@tanstack/react-query'

import { getCodeTypeHierarchy } from '../services/codeTypeHierarchy'
import type { DefinitionLocation } from '../types/api'

/** Find a class's type hierarchy on demand only; results are never cached or stored. */
export function useCodeTypeHierarchy(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: (definition: DefinitionLocation) =>
      getCodeTypeHierarchy(projectId, repositoryId, definition.path, definition.line),
  })
}
