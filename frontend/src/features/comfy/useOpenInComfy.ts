/**
 * "Open in ComfyUI", in one place.
 *
 * The framework asks the backend for a deep link — which also makes sure ComfyUI has the workflow saved
 * under a name, so it opens as a real saved workflow rather than an untitled one — and then shows it in
 * the embedded ComfyUI panel instead of throwing the user into another browser tab.
 *
 * Holding shift still opens a real tab, because a second window is genuinely better on a second monitor.
 */

import { useCallback } from 'react'

import { api, ApiError } from '@/api/client'
import { useStudio } from '@/store/studio'
import { useLayout } from '@/store/layout'
import { useToast } from '@/components/ui'

export function useOpenInComfy() {
  const toast = useToast()
  const showInComfy = useStudio((s) => s.showInComfy)
  const showWidget = useLayout((s) => s.showWidget)

  return useCallback(
    async (projectId: string, workflowId: string, options: { newTab?: boolean } = {}) => {
      try {
        const result = await api.workflows.openInComfy(projectId, workflowId)
        // A hint means the deep link works but something about it is worth saying — the workflow could
        // not be saved into ComfyUI, say. Not fatal, so the panel still opens.
        if (result.hint) toast.push('bad', result.hint)

        if (options.newTab) {
          window.open(result.url, '_blank', 'noopener')
          return result
        }

        showWidget('comfy')
        showInComfy(result.url)
        return result
      } catch (error) {
        toast.push('bad', (error as ApiError).message)
        return null
      }
    },
    [showInComfy, showWidget, toast],
  )
}
