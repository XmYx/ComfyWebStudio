/**
 * Builds the {@link CommandContext} that commands run against.
 *
 * Extracted so the menu bar, every right-click menu and the keyboard handler all resolve "what is
 * selected right now" the same way — a command must behave identically however it was invoked.
 */

import { useMemo } from 'react'
import { useMatch, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'
import { useStudio } from '@/store/studio'
import { useToast } from '@/components/ui'
import type { CommandContext } from './commands'

export function useCommandContext(): CommandContext {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()
  const match = useMatch('/p/:projectId/*')
  const projectId = match?.params.projectId

  const shotId = useStudio((s) => s.shotId)
  const selectedStepId = useStudio((s) => s.selectedStepId)

  const { data: project } = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => api.projects.get(projectId!),
    enabled: Boolean(projectId),
  })

  const { data: history } = useQuery({
    queryKey: ['history', projectId, project?.modified],
    queryFn: () => api.projects.history(projectId!),
    enabled: Boolean(projectId),
  })

  return useMemo(() => {
    const shot = project?.shots.find((s) => s.id === shotId) ?? project?.shots[0] ?? null
    return {
      project: project ?? null,
      shot,
      step: shot?.steps.find((s) => s.id === selectedStepId) ?? null,
      navigate,
      queryClient,
      toast: toast.push,
      history: history ?? { undo: 0, redo: 0 },
      refresh: () => {
        queryClient.invalidateQueries({ queryKey: ['project', projectId] })
        queryClient.invalidateQueries({ queryKey: ['history', projectId] })
        queryClient.invalidateQueries({ queryKey: ['versions', projectId] })
        queryClient.invalidateQueries({ queryKey: ['projects'] })
      },
    }
  }, [project, shotId, selectedStepId, navigate, queryClient, toast, history, projectId])
}
