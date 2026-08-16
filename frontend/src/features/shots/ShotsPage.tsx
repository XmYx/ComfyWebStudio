import { useEffect, useMemo } from 'react'
import { useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { RunMode, Shot } from '@/api/types'
import { useStudio } from '@/store/studio'
import { selectWidgets, useLayout } from '@/store/layout'
import { ShotCanvas } from '@/features/graph/ShotCanvas'
import { ComfyPanel } from '@/features/comfy/ComfyPanel'
import { ShotList } from './ShotList'
import { WorkflowLibrary } from './WorkflowLibrary'
import { AssetLibrary } from './AssetLibrary'
import { StepInspector } from './StepInspector'
import { InstanceInspector } from './InstanceInspector'
import { TemplateEditor } from '@/features/graph/TemplateEditor'
import { Dock } from '@/features/shell/Dock'
import { TimelinePage } from '@/features/timeline/TimelinePage'
import { Monitor } from '@/features/timeline/Monitor'
import {
  Badge, Button, Callout, Empty, Panel, PanelHeader, Spinner, useToast,
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
  const queue = useStudio((s) => s.queue)
  const selectedShotIds = useStudio((s) => s.selectedShotIds)
  const setSelectedShots = useStudio((s) => s.setSelectedShots)
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

  // No ids means every shot, which is what the backend takes an empty list to mean too.
  const startBatch = useMutation({
    mutationFn: ({ shotIds, force }: { shotIds: string[]; force?: boolean }) =>
      api.runs.startBatch(projectId!, shotIds, force ?? false),
    onError: (err: ApiError) => toast.push('bad', err.message),
  })

  const cancelBatch = useMutation({
    mutationFn: (batchId: string) => api.runs.cancelBatch(projectId!, batchId),
    onError: (err: ApiError) => toast.push('bad', err.message),
  })

  // A queue outlives the page that started it, so a reload rejoins the one already running rather than
  // showing an idle panel over a ComfyUI that is very much busy.
  const { data: runningBatch } = useQuery({
    queryKey: ['batch-active', projectId],
    queryFn: () => api.runs.activeBatch(projectId!),
    enabled: Boolean(projectId),
  })

  useEffect(() => {
    if (!runningBatch || useStudio.getState().queue) return
    const finished = runningBatch.shots.filter((s) => s.status !== 'queued' && s.status !== 'running')
    useStudio.getState().beginQueue({
      id: runningBatch.id,
      shotIds: runningBatch.shots.map((s) => s.shot_id),
      done: Object.fromEntries(finished.map((s) => [s.shot_id, s.status])),
      current: runningBatch.shots.find((s) => s.status === 'running')?.shot_id ?? null,
    })
    // The statuses too, not just the queue: this page missed the run events for everything the batch
    // got through before it was opened, and a shot that failed ten minutes ago should still say so.
    for (const entry of finished) {
      useStudio.getState().adoptShotRun(entry.shot_id, entry.run_id, entry.status, entry.error)
    }
  }, [runningBatch])

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

  /**
   * Which shots an operation covers: the ticked ones when the shot under the pointer is among them,
   * otherwise just that one.
   *
   * Right-clicking a shot outside the selection acts on *that* shot rather than on the ticked set. The
   * alternative — the menu quietly operating on three shots elsewhere in the list — is the kind of
   * surprise that loses work.
   */
  const targetsFor = (candidate: Shot): Shot[] => {
    const ticked = shots.filter((s) => selectedShotIds.includes(s.id))
    return ticked.length && ticked.some((s) => s.id === candidate.id) ? ticked : [candidate]
  }

  const many = (list: unknown[], one: string, more: string) =>
    list.length === 1 ? one : `${list.length} ${more}`

  const duplicateShots = async (targets: Array<{ id: string; name: string }>) => {
    try {
      let last = ''
      // In list order and one at a time: each duplicate is its own save, so a failure halfway leaves
      // the copies that succeeded rather than an ambiguous half-batch.
      for (const target of targets) last = (await api.shots.duplicate(project.id, target.id)).id
      if (last) setShot(last)
      setSelectedShots([])
      invalidate()
      toast.push('ok', `Duplicated ${many(targets, `“${targets[0].name}”`, 'shots')}.`)
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    }
  }

  const deleteShots = async (targets: Array<{ id: string; name: string }>) => {
    const what = many(targets, `“${targets[0].name}”`, 'shots')
    if (!confirm(`Delete ${what} and their steps?`)) return
    try {
      for (const target of targets) await api.shots.remove(project.id, target.id)
      setShot(null)
      setSelectedShots([])
      invalidate()
      toast.push('ok', `Deleted ${what}.`)
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    }
  }

  const copyShots = (targets: Array<{ id: string; name: string }>) => {
    useLayout.getState().setClipboard({
      kind: 'shots',
      payload: targets.map((t) => t.id),
      label: many(targets, targets[0].name, 'shots'),
    })
    toast.push('info', `Copied ${many(targets, `“${targets[0].name}”`, 'shots')}.`)
  }

  /** Pasting a shot is asking the server to duplicate it — see the note on `Clipboard`. */
  const pasteShots = async () => {
    const clipboard = useLayout.getState().clipboard
    if (!clipboard || clipboard.kind !== 'shots') return
    const ids = clipboard.payload as string[]
    const alive = shots.filter((s) => ids.includes(s.id))
    if (!alive.length) {
      toast.push('bad', 'The copied shots are no longer in this project.')
      return
    }
    await duplicateShots(alive)
    if (alive.length < ids.length) {
      toast.push('info', `${ids.length - alive.length} copied shot(s) had been deleted.`)
    }
  }

  const shotMenu = (candidate: (typeof project.shots)[number]): MenuItem[] => {
    const targets = targetsFor(candidate)
    const scope = many(targets, candidate.name, 'shots selected')
    const clipboard = useLayout.getState().clipboard
    return [
    { type: 'header', label: scope },
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
      label: `Duplicate ${many(targets, 'shot', 'shots')}`,
      shortcut: 'Mod+D',
      onSelect: () => void duplicateShots(targets),
    },
    { type: 'separator' },
    {
      type: 'action',
      label: `Copy ${many(targets, 'shot', 'shots')}`,
      shortcut: 'Mod+C',
      onSelect: () => copyShots(targets),
    },
    {
      type: 'action',
      label: clipboard?.kind === 'shots' ? `Paste ${clipboard.label}` : 'Paste',
      shortcut: 'Mod+V',
      disabled: clipboard?.kind !== 'shots',
      onSelect: () => void pasteShots(),
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
      label: `Delete ${many(targets, 'shot', 'shots')}`,
      shortcut: 'Delete',
      danger: true,
      onSelect: () => void deleteShots(targets),
    },
  ]
  }

  // Each panel is a widget the Dock places — docked into a slot, or floating in a window of its own.
  const shotsPanel = (
    <ShotList
      shots={shots}
      currentId={shot?.id ?? null}
      onOpen={setShot}
      onCreate={() => createShot.mutate()}
      onRender={(ids) => startBatch.mutate({ shotIds: ids })}
      onStop={() => queue && cancelBatch.mutate(queue.id)}
      onContextMenu={(event, candidate) => {
        setShot(candidate.id)
        contextMenu.open(event, shotMenu(candidate))
      }}
    />
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
              {queue ? (
                // A queue owns the ComfyUI for its whole length. Cancelling only the shot in flight
                // would stop this one and start the next, which is not what a button here would mean.
                <Button
                  size="sm"
                  variant="danger"
                  onClick={() => cancelBatch.mutate(queue.id)}
                  title="Stop the render queue"
                >
                  <Spinner /> Stop queue
                </Button>
              ) : running ? (
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
