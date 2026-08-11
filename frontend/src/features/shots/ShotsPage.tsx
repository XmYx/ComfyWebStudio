import { useEffect, useMemo } from 'react'
import { useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { RunMode } from '@/api/types'
import { useStudio } from '@/store/studio'
import { selectWidgets, useLayout } from '@/store/layout'
import { ShotCanvas } from '@/features/graph/ShotCanvas'
import { ComfyPanel } from '@/features/comfy/ComfyPanel'
import { WorkflowLibrary } from './WorkflowLibrary'
import { AssetLibrary } from './AssetLibrary'
import { startDrag } from '@/lib/dnd'
import { StepInspector } from './StepInspector'
import { InstanceInspector } from './InstanceInspector'
import { TemplateEditor } from '@/features/graph/TemplateEditor'
import { Dock } from '@/features/shell/Dock'
import { TimelinePage } from '@/features/timeline/TimelinePage'
import { Monitor } from '@/features/timeline/Monitor'
import {
  Badge, Button, Callout, Empty, Panel, PanelHeader, Spinner, cx, useToast,
} from '@/components/ui'
import { ContextMenu, useContextMenu, type MenuItem } from '@/components/ContextMenu'
import { useCommandContext } from '@/features/menu/useCommandContext'

export function ShotsPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const toast = useToast()
  const queryClient = useQueryClient()

  const shotId = useStudio((s) => s.shotId)
  const setShot = useStudio((s) => s.setShot)
  const selectedStepId = useStudio((s) => s.selectedStepId)
  const selectedInstanceId = useStudio((s) => s.selectedInstanceId)
  const openInstanceId = useStudio((s) => s.openInstanceId)
  const openInstance_ = useStudio((s) => s.openInstance)
  const activeRun = useStudio((s) => s.activeRun)
  const seedFromResults = useStudio((s) => s.seedFromResults)
  // The transport is shared, so a floating Monitor widget follows the same playhead as the timeline.
  const playhead = useStudio((s) => s.playhead)
  const setPlayhead = useStudio((s) => s.setPlayhead)
  const playing = useStudio((s) => s.playing)
  const setPlaying = useStudio((s) => s.setPlaying)
  const commandContext = useCommandContext()
  const contextMenu = useContextMenu()

  // The route decides which layout is on screen, so every panel action lands on the right one.
  const setWorkspace = useLayout((s) => s.setWorkspace)
  useEffect(() => setWorkspace('shots'), [setWorkspace])

  const { data: project, isLoading, error } = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => api.projects.get(projectId!),
    enabled: Boolean(projectId),
  })

  // An open template editing session lives in `project.shots` but is not one of the user's shots, so it
  // never appears in the list and is never what gets selected.
  const shots = useMemo(
    () => (project?.shots ?? []).filter((s) => !s.template_edit_id),
    [project],
  )

  const shot = useMemo(
    () => shots.find((s) => s.id === shotId) ?? shots[0] ?? null,
    [shots, shotId],
  )

  // Select the first shot automatically so the canvas is never pointlessly empty.
  useEffect(() => {
    if (project && !shotId && shots.length) setShot(shots[0].id)
  }, [project, shotId, shots, setShot])

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

  // Shared with the canvas, which asks for the same thing — TanStack dedupes it to one request.
  const { data: placedList } = useQuery({
    queryKey: ['placed', projectId, shot?.id, project?.modified],
    queryFn: () => api.instances.placed(projectId!, shot!.id),
    enabled: Boolean(projectId && shot?.instances.length),
  })
  const placedById = useMemo(
    () => new Map((placedList ?? []).map((entry) => [entry.instance_id, entry])),
    [placedList],
  )

  // Looking inside a placed *shot* just means going to that shot. Unlike a template it is already a real
  // shot with a real canvas, so there is no editing session to open — and edits made there are meant to
  // reach every node standing for it, which is what editing the shot directly does.
  const drilledIntoShot = openInstanceId
    ? placedById.get(openInstanceId)?.source_shot_id ?? null
    : null
  useEffect(() => {
    if (!drilledIntoShot) return
    openInstance_(null)
    setShot(drilledIntoShot)
  }, [drilledIntoShot, openInstance_, setShot])

  // Only fetched when a Monitor is actually on screen — it is off by default.
  const monitorVisible = useLayout((s) => selectWidgets(s).monitor.visible)
  const { data: resolvedTimeline } = useQuery({
    queryKey: ['timeline-resolved', projectId, project?.modified],
    queryFn: () => api.timeline.resolved(projectId!),
    enabled: Boolean(projectId && monitorVisible),
  })

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
    mutationFn: () => api.shots.create(projectId!, `Shot ${shots.length + 1}`),
    onSuccess: (created) => {
      setShot(created.id)
      invalidate()
    },
  })

  if (isLoading) return <Empty title="Loading project…" />
  if (error) return <div className="p-6"><Callout tone="bad">{(error as ApiError).message}</Callout></div>
  if (!project) return null

  const selectedStep = shot?.steps.find((s) => s.id === selectedStepId) ?? null
  const selectedInstance = shot?.instances.find((i) => i.id === selectedInstanceId) ?? null
  const openInstance = shot?.instances.find((i) => i.id === openInstanceId) ?? null
  const running = activeRun?.status === 'running' || startRun.isPending

  const shotMenu = (candidate: (typeof project.shots)[number]): MenuItem[] => [
    { type: 'header', label: candidate.name },
    { type: 'action', label: 'Run shot', onSelect: () => startRun.mutate({ mode: 'shot' }) },
    {
      type: 'action',
      label: 'Run, ignoring cached results',
      onSelect: () => startRun.mutate({ mode: 'shot', force: true }),
    },
    { type: 'separator' },
    {
      type: 'action',
      label: 'Rename…',
      onSelect: async () => {
        const name = prompt('Shot name', candidate.name)
        if (name && name !== candidate.name) {
          await api.shots.update(project.id, candidate.id, { name })
          invalidate()
        }
      },
    },
    {
      type: 'action',
      label: 'Duplicate shot',
      onSelect: async () => {
        const copy = await api.shots.duplicate(project.id, candidate.id)
        setShot(copy.id)
        invalidate()
      },
    },
    { type: 'separator' },
    {
      type: 'action',
      label: 'Save a named version…',
      onSelect: async () => {
        const label = prompt(`Name this version of “${candidate.name}”`, '')
        if (!label) return
        await api.versions.tagShot(project.id, candidate.id, label)
        toast.push('ok', `Saved version “${label}”.`)
      },
    },
    {
      type: 'action',
      label: 'Show history…',
      onSelect: () => {
        useStudio.getState().setHistoryTarget({ scope: 'shot', id: candidate.id, name: candidate.name })
        useLayout.getState().openDialog('history')
      },
    },
    { type: 'separator' },
    {
      type: 'action',
      label: 'Delete shot',
      danger: true,
      onSelect: async () => {
        if (!confirm(`Delete “${candidate.name}” and its steps?`)) return
        await api.shots.remove(project.id, candidate.id)
        setShot(null)
        invalidate()
      },
    },
  ]

  // Each panel is a widget the Dock places — docked into a slot, or floating in a window of its own.
  const shotsPanel = (
    <Panel>
      <PanelHeader
        actions={<Button size="sm" onClick={() => createShot.mutate()}>+</Button>}
      >
        Shots
      </PanelHeader>
      <div className="max-h-48 overflow-y-auto p-1">
        {!shots.length ? (
          <div className="p-2 text-xs text-[var(--color-ink-dim)]">
            Create a shot to begin.
          </div>
        ) : (
          shots.map((candidate) => (
            <button
              key={candidate.id}
              draggable
              // Dragging a shot onto another shot's canvas makes a node supplying its last result.
              onDragStart={(event) =>
                startDrag(event, { kind: 'shot', id: candidate.id, name: candidate.name })
              }
              title="Click to open, or drag onto a canvas to use its output"
              onClick={() => setShot(candidate.id)}
              onContextMenu={(event) => {
                setShot(candidate.id)
                contextMenu.open(event, shotMenu(candidate))
              }}
              className={cx(
                'flex w-full items-center justify-between rounded px-2 py-1.5 text-left text-xs transition-colors',
                candidate.id === shot?.id
                  ? 'bg-[var(--color-panel-2)] text-[var(--color-ink)]'
                  : 'text-[var(--color-ink-dim)] hover:bg-[var(--color-panel-2)]/60',
              )}
            >
              <span className="truncate">{candidate.name}</span>
              {/* Placed templates count too — a shot made only of them is not an empty shot. */}
              <span className="text-[10px]">
                {candidate.steps.length + candidate.instances.length}
              </span>
            </button>
          ))
        )}
      </div>
    </Panel>
  )

  const canvasPanel = (
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
                    disabled={!shot.steps.length && !shot.instances.length}
                    onClick={() => startRun.mutate({ mode: 'shot' })}
                  >
                    ▶ Run shot
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    title="Ignore cached results and re-execute everything"
                    disabled={!shot.steps.length && !shot.instances.length}
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
        {openInstance && shot && !drilledIntoShot ? (
          // Drilled into a placed template: the canvas edits the template itself until the user leaves.
          <TemplateEditor
            project={project}
            parentShotName={shot.name}
            templateId={openInstance.template_id}
            label={openInstance.name || placedById.get(openInstance.id)?.summary?.name || 'Template'}
            onChanged={invalidate}
            onRunStep={(stepId) => startRun.mutate({ mode: 'step', step_ids: [stepId] })}
          />
        ) : shot ? (
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
  )

  const inspectorPanel = (
    <div className="min-h-0">
      {selectedInstance && shot ? (
        <InstanceInspector
          project={project}
          shot={shot}
          instance={selectedInstance}
          placed={placedById.get(selectedInstance.id)}
          onChanged={invalidate}
          onRun={(stepIds: string[]) =>
            stepIds.length
              ? startRun.mutate({ mode: 'step', step_ids: stepIds })
              : startRun.mutate({ mode: 'shot' })
          }
        />
      ) : selectedStep && shot ? (
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
          <Empty title="Nothing selected">
            Click a step or a placed template on the canvas to edit it and see its output.
          </Empty>
        </Panel>
      )}
    </div>
  )

  return (
    <>
      <Dock
        render={{
          shots: shotsPanel,
          workflows: <WorkflowLibrary project={project} shot={shot} onChanged={invalidate} />,
          assets: <AssetLibrary project={project} onChanged={invalidate} />,
          canvas: canvasPanel,
          comfy: <ComfyPanel />,
          // The timeline is a centre widget too, so floating one of the two shows both at once.
          timeline: <TimelinePage embedded />,
          inspector: inspectorPanel,
          monitor: (
            <Panel className="flex h-full min-h-0 flex-col overflow-hidden">
              <PanelHeader>Monitor</PanelHeader>
              <Monitor
                className="min-h-0 flex-1"
                project={project}
                resolved={resolvedTimeline}
                playhead={playhead}
                onScrub={setPlayhead}
                playing={playing}
                onPlayingChange={setPlaying}
              />
            </Panel>
          ),
        }}
      />
      <ContextMenu state={contextMenu.menu} onClose={contextMenu.close} context={commandContext} />
    </>
  )
}
