import { useEffect, useMemo } from 'react'
import { useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { RunMode } from '@/api/types'
import { useStudio } from '@/store/studio'
import { useLayout } from '@/store/layout'
import { ShotCanvas } from '@/features/graph/ShotCanvas'
import { WorkflowLibrary } from './WorkflowLibrary'
import { StepInspector } from './StepInspector'
import {
  Badge, Button, Callout, Empty, Panel, PanelHeader, Spinner, cx, useToast,
} from '@/components/ui'

export function ShotsPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const toast = useToast()
  const queryClient = useQueryClient()

  const shotId = useStudio((s) => s.shotId)
  const setShot = useStudio((s) => s.setShot)
  const selectedStepId = useStudio((s) => s.selectedStepId)
  const activeRun = useStudio((s) => s.activeRun)
  const seedFromResults = useStudio((s) => s.seedFromResults)
  const showLeftPanel = useLayout((s) => s.showLeftPanel)
  const showInspector = useLayout((s) => s.showInspector)

  const { data: project, isLoading, error } = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => api.projects.get(projectId!),
    enabled: Boolean(projectId),
  })

  const shot = useMemo(
    () => project?.shots.find((s) => s.id === shotId) ?? project?.shots[0] ?? null,
    [project, shotId],
  )

  // Select the first shot automatically so the canvas is never pointlessly empty.
  useEffect(() => {
    if (project && !shotId && project.shots.length) setShot(project.shots[0].id)
  }, [project, shotId, setShot])

  // Repopulate previews from the last successful run whenever the shot changes, so reopening a project
  // shows results rather than blank cards.
  const { data: results } = useQuery({
    queryKey: ['results', projectId, shot?.id],
    queryFn: () => api.shots.results(projectId!, shot!.id),
    enabled: Boolean(projectId && shot),
  })

  useEffect(() => {
    if (results) seedFromResults(results)
  }, [results, seedFromResults])

  const { data: report } = useQuery({
    queryKey: ['validate', projectId, shot?.id, project?.modified],
    queryFn: () => api.shots.validate(projectId!, shot!.id),
    enabled: Boolean(projectId && shot),
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['project', projectId] })
  }

  const startRun = useMutation({
    mutationFn: (body: { mode: RunMode; step_ids?: string[]; force?: boolean }) =>
      api.runs.start(projectId!, shot!.id, body),
    onError: (err: ApiError) => {
      const issues = (err.details as any)?.issues as Array<{ message: string }> | undefined
      toast.push('bad', issues?.length ? issues.map((i) => i.message).join(' ') : err.message)
    },
  })

  const cancelRun = useMutation({
    mutationFn: () => api.runs.cancel(projectId!, activeRun!.id),
  })

  const createShot = useMutation({
    mutationFn: () => api.shots.create(projectId!, `Shot ${(project?.shots.length ?? 0) + 1}`),
    onSuccess: (created) => {
      setShot(created.id)
      invalidate()
    },
  })

  if (isLoading) return <Empty title="Loading project…" />
  if (error) return <div className="p-6"><Callout tone="bad">{(error as ApiError).message}</Callout></div>
  if (!project) return null

  const selectedStep = shot?.steps.find((s) => s.id === selectedStepId) ?? null
  const running = activeRun?.status === 'running' || startRun.isPending

  return (
    <div
      className="grid h-full gap-2 p-2"
      style={{
        // Hiding a panel collapses its column entirely, so the canvas actually gains the space.
        gridTemplateColumns: [
          showLeftPanel ? '260px' : null,
          '1fr',
          showInspector ? '340px' : null,
        ].filter(Boolean).join(' '),
      }}
    >
      {/* Left: shots and workflow library */}
      {showLeftPanel && (
      <div className="grid min-h-0 grid-rows-[auto_1fr] gap-2">
        <Panel>
          <PanelHeader
            actions={<Button size="sm" onClick={() => createShot.mutate()}>+</Button>}
          >
            Shots
          </PanelHeader>
          <div className="max-h-48 overflow-y-auto p-1">
            {!project.shots.length ? (
              <div className="p-2 text-xs text-[var(--color-ink-dim)]">
                Create a shot to begin.
              </div>
            ) : (
              project.shots.map((candidate) => (
                <button
                  key={candidate.id}
                  onClick={() => setShot(candidate.id)}
                  className={cx(
                    'flex w-full items-center justify-between rounded px-2 py-1.5 text-left text-xs transition-colors',
                    candidate.id === shot?.id
                      ? 'bg-[var(--color-panel-2)] text-[var(--color-ink)]'
                      : 'text-[var(--color-ink-dim)] hover:bg-[var(--color-panel-2)]/60',
                  )}
                >
                  <span className="truncate">{candidate.name}</span>
                  <span className="text-[10px]">{candidate.steps.length}</span>
                </button>
              ))
            )}
          </div>
        </Panel>

        <WorkflowLibrary project={project} shot={shot} onChanged={invalidate} />
      </div>
      )}

      {/* Centre: the canvas */}
      <Panel className="flex min-h-0 flex-col overflow-hidden">
        <PanelHeader
          actions={
            shot && (
              <>
                {report && !report.ok && (
                  <Badge tone="bad" title={report.issues.map((i) => i.message).join('\n')}>
                    {report.issues.filter((i) => i.level === 'error').length} issue(s)
                  </Badge>
                )}
                {running ? (
                  <Button size="sm" variant="danger" onClick={() => cancelRun.mutate()}>
                    <Spinner /> Cancel
                  </Button>
                ) : (
                  <>
                    <Button
                      size="sm"
                      variant="primary"
                      disabled={!shot.steps.length}
                      onClick={() => startRun.mutate({ mode: 'shot' })}
                    >
                      ▶ Run shot
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      title="Ignore cached results and re-execute everything"
                      disabled={!shot.steps.length}
                      onClick={() => startRun.mutate({ mode: 'shot', force: true })}
                    >
                      Force
                    </Button>
                  </>
                )}
              </>
            )
          }
        >
          {shot?.name ?? 'No shot'}
        </PanelHeader>

        {report && report.issues.some((i) => i.level === 'error') && (
          <div className="border-b border-[var(--color-edge)] p-2">
            <Callout tone="bad" title="This shot cannot run yet">
              <ul className="list-disc space-y-0.5 pl-4">
                {report.issues
                  .filter((i) => i.level === 'error')
                  .slice(0, 4)
                  .map((issue, index) => <li key={index}>{issue.message}</li>)}
              </ul>
            </Callout>
          </div>
        )}

        <div className="min-h-0 flex-1">
          {shot ? (
            <ShotCanvas
              project={project}
              shot={shot}
              onChanged={invalidate}
              onRunStep={(stepId) => startRun.mutate({ mode: 'step', step_ids: [stepId] })}
            />
          ) : (
            <Empty title="No shot selected">Create a shot from the panel on the left.</Empty>
          )}
        </div>
      </Panel>

      {/* Right: inspector */}
      {showInspector && (
      <div className="min-h-0">
        {selectedStep && shot ? (
          <StepInspector
            project={project}
            shot={shot}
            step={selectedStep}
            onChanged={invalidate}
            onRunStep={(stepId, mode) => startRun.mutate({ mode, step_ids: [stepId] })}
          />
        ) : (
          <Panel className="h-full">
            <PanelHeader>Inspector</PanelHeader>
            <Empty title="No step selected">
              Click a step on the canvas to edit its parameters and see its output.
            </Empty>
          </Panel>
        )}
      </div>
      )}
    </div>
  )
}
