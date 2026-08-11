/**
 * The timeline as a workspace rather than a fixed page.
 *
 * It is the same dock the shot editor uses — panels split, tab, float, maximise and remember where they
 * were — over a different set of panels and its own saved arrangement. Cutting wants the viewer above the
 * tracks and the sources to hand; editing shots wants a canvas and an inspector. One layout could only
 * ever be a compromise between the two, so each route gets its own.
 */

import { useEffect } from 'react'
import { useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'
import { useLayout } from '@/store/layout'
import { useStudio } from '@/store/studio'
import { Empty, Panel, PanelHeader } from '@/components/ui'
import { Dock } from '@/features/shell/Dock'
import { AssetLibrary } from '@/features/shots/AssetLibrary'
import { Monitor } from './Monitor'
import { ClipInspector, RendersPanel, TimelinePage } from './TimelinePage'
import { TimelineShotList } from './TimelineShotList'

export function TimelineWorkspace() {
  const { projectId } = useParams<{ projectId: string }>()
  const queryClient = useQueryClient()

  // The route decides which layout is on screen, so every panel action lands on the right one.
  const setWorkspace = useLayout((s) => s.setWorkspace)
  useEffect(() => setWorkspace('timeline'), [setWorkspace])

  const playhead = useStudio((s) => s.playhead)
  const setPlayhead = useStudio((s) => s.setPlayhead)
  const playing = useStudio((s) => s.playing)
  const setPlaying = useStudio((s) => s.setPlaying)

  const { data: project } = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => api.projects.get(projectId!),
    enabled: Boolean(projectId),
  })

  const { data: resolved } = useQuery({
    queryKey: ['timeline-resolved', projectId, project?.modified],
    queryFn: () => api.timeline.resolved(projectId!),
    enabled: Boolean(projectId),
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['project', projectId] })
    queryClient.invalidateQueries({ queryKey: ['timeline-resolved', projectId] })
  }

  if (!project) return <Empty title="Loading…" />

  return (
    <Dock
      render={{
        // The page's own toolbar and tracks; its dialogs and context menus come with it.
        timeline: <TimelinePage embedded />,
        shots: <TimelineShotList project={project} onChanged={invalidate} />,
        assets: <AssetLibrary project={project} onChanged={invalidate} />,
        monitor: (
          <Panel className="flex h-full min-h-0 flex-col overflow-hidden">
            <PanelHeader>Monitor</PanelHeader>
            <Monitor
              className="min-h-0 flex-1"
              project={project}
              resolved={resolved}
              playhead={playhead}
              onScrub={setPlayhead}
              playing={playing}
              onPlayingChange={setPlaying}
            />
          </Panel>
        ),
        inspector: <ClipInspector project={project} onChanged={invalidate} />,
        renders: <RendersPanel project={project} />,
      }}
    />
  )
}
