/**
 * The storyboard workspace.
 *
 * The same dock as the other two routes, over the three panels this work needs: the premise and its
 * bindings, the frames, and the people. It is its own workspace rather than a panel bolted onto the shot
 * editor because writing a board and cutting a sequence want completely different things on screen.
 */

import { useEffect } from 'react'
import { useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import { useLayout } from '@/store/layout'
import { Button, Empty, Panel, Spinner, useToast } from '@/components/ui'
import { Dock } from '@/features/shell/Dock'
import { AssetLibrary } from '@/features/shots/AssetLibrary'
import { CharactersPanel } from './CharactersPanel'
import { FrameStrip } from './FrameStrip'
import { PipelinePanel } from './PipelinePanel'
import { StoryboardSetup } from './StoryboardSetup'

export function StoryboardWorkspace() {
  const { projectId } = useParams<{ projectId: string }>()
  const queryClient = useQueryClient()
  const toast = useToast()

  const setWorkspace = useLayout((s) => s.setWorkspace)
  useEffect(() => setWorkspace('storyboard'), [setWorkspace])

  const { data: project } = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => api.projects.get(projectId!),
    enabled: Boolean(projectId),
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['project', projectId] })
    queryClient.invalidateQueries({ queryKey: ['board-surfaces'] })
  }

  const create = useMutation({
    mutationFn: () =>
      api.storyboards.create(projectId!, { name: 'Storyboard', premise: '' }),
    onSuccess: invalidate,
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  if (!project) return <Empty title="Loading…" />

  // One board per project for now: a second one is a rare thing to want, and picking between them would
  // cost a selector on every panel for no benefit yet.
  const board = project.storyboards?.[0]
  if (!board) {
    return (
      <div className="grid h-full place-items-center p-6">
        <Panel className="max-w-md p-6 text-center">
          <div className="mb-2 text-sm font-semibold">No storyboard yet</div>
          <p className="mb-4 text-xs leading-relaxed text-[var(--color-ink-dim)]">
            Describe what you want to make, and a language model breaks it into shots. Each one becomes a
            still you can look at, and then a shot that renders.
          </p>
          <Button disabled={create.isPending} onClick={() => create.mutate()}>
            {create.isPending ? <Spinner /> : null} Start a storyboard
          </Button>
        </Panel>
      </div>
    )
  }

  return (
    <Dock
      render={{
        inspector: <StoryboardSetup project={project} board={board} onChanged={invalidate} />,
        pipeline: <PipelinePanel project={project} board={board} onChanged={invalidate} />,
        storyboard: <FrameStrip project={project} board={board} onChanged={invalidate} />,
        characters: <CharactersPanel project={project} board={board} onChanged={invalidate} />,
        assets: <AssetLibrary project={project} onChanged={invalidate} />,
      }}
    />
  )
}
