import { useEffect } from 'react'
import { Navigate, Route, Routes, useParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'

import { eventStream, useStudioEvents } from '@/api/events'
import { useStudio } from '@/store/studio'
import { Header } from '@/features/shell/Header'
import { MenuBar } from '@/features/menu/MenuBar'
import { AppDialogs } from '@/features/menu/Dialogs'
import { ProjectsPage } from '@/features/projects/ProjectsPage'
import { ShotsPage } from '@/features/shots/ShotsPage'
import { TimelinePage } from '@/features/timeline/TimelinePage'
import { SettingsPage } from '@/features/settings/SettingsPage'
import { useToast } from '@/components/ui'

/**
 * Wires the live event stream to the project the user has open, and translates events into store updates
 * and cache invalidations. Doing this once here keeps every feature component free of subscription logic.
 */
function ProjectShell({ children }: { children: React.ReactNode }) {
  const { projectId } = useParams<{ projectId: string }>()
  const setProject = useStudio((s) => s.setProject)
  const queryClient = useQueryClient()
  const toast = useToast()

  useEffect(() => {
    setProject(projectId ?? null)
    eventStream.connect(projectId ?? null)
  }, [projectId, setProject])

  useStudioEvents((event) => {
    const store = useStudio.getState()

    switch (event.type) {
      case 'run.started':
        store.beginRun({
          id: event.run_id!,
          shot_id: event.data.shot_id,
          mode: event.data.mode,
          status: 'running',
          started: new Date().toISOString(),
          finished: null,
          step_runs: (event.data.steps ?? []).map((s: any) => ({ step_id: s.id, status: 'pending' })),
          error: null,
        } as any)
        break

      case 'step.started':
        store.patchStep(event.step_id!, { status: 'running', progress: 0 })
        break

      case 'step.progress':
        store.patchStep(event.step_id!, { status: 'running', progress: event.data.progress ?? 0 })
        break

      case 'step.finished':
        store.patchStep(event.step_id!, {
          status: event.data.status,
          progress: 1,
          outputs: event.data.outputs,
          cached: event.data.status === 'cached',
        })
        break

      case 'step.failed':
        store.patchStep(event.step_id!, { status: 'error', progress: 1, error: event.data.error })
        toast.push('bad', event.data.error ?? 'A step failed.')
        break

      case 'run.finished':
        store.endRun(event.data.status, event.data.error)
        if (event.data.status === 'success') toast.push('ok', 'Run finished.')
        else if (event.data.status !== 'cancelled') toast.push('bad', event.data.error ?? 'Run failed.')
        queryClient.invalidateQueries({ queryKey: ['runs', projectId] })
        break

      case 'run.cancelled':
        store.endRun('cancelled')
        toast.push('info', 'Run cancelled.')
        break

      case 'workflow.synced': {
        const removed: string[] = event.data.removed_ports ?? []
        const broken: unknown[] = event.data.broken_links ?? []
        toast.push(
          removed.length ? 'bad' : 'ok',
          removed.length
            ? `${event.data.name}: synced, but ${removed.length} port(s) were removed and ` +
              `${broken.length} link(s) were disconnected.`
            : `${event.data.name ?? 'Workflow'} synced from ComfyUI — ${event.data.ports} port(s).`,
        )
        queryClient.invalidateQueries({ queryKey: ['project', projectId] })
        queryClient.invalidateQueries({ queryKey: ['workflows', projectId] })
        break
      }

      case 'render.progress':
        store.setRenderProgress({
          id: event.data.render_id,
          progress: event.data.progress ?? 0,
          message: event.data.message ?? '',
        })
        break

      case 'render.finished':
        store.setRenderProgress(null)
        if (event.data.ok) {
          toast.push('ok', `Render complete: ${event.data.kind}`)
          queryClient.invalidateQueries({ queryKey: ['renders', projectId] })
        } else {
          toast.push('bad', `Render failed: ${event.data.error}`)
        }
        break

      case 'project.changed':
        queryClient.invalidateQueries({ queryKey: ['project', projectId] })
        break
    }
  }, [projectId])

  return <>{children}</>
}

export default function App() {
  return (
    <div className="flex h-full flex-col">
      <MenuBar />
      <Header />
      <main className="min-h-0 flex-1">
        <Routes>
          <Route path="/" element={<Navigate to="/projects" replace />} />
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route
            path="/p/:projectId/shots"
            element={<ProjectShell><ShotsPage /></ProjectShell>}
          />
          <Route
            path="/p/:projectId/timeline"
            element={<ProjectShell><TimelinePage /></ProjectShell>}
          />
          <Route path="/p/:projectId" element={<RedirectToShots />} />
          <Route path="*" element={<Navigate to="/projects" replace />} />
        </Routes>
      </main>
      <AppDialogs />
    </div>
  )
}

function RedirectToShots() {
  const { projectId } = useParams()
  return <Navigate to={`/p/${projectId}/shots`} replace />
}
