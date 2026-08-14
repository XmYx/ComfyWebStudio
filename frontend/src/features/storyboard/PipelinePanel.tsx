/**
 * The flow, as a list of steps you can read, edit, run and watch.
 *
 * Everything the storyboard does is one of these rows. Before this, the order was implicit in which
 * buttons existed and the wording was in the source — so a step that misbehaved was something to file a
 * bug about rather than something to fix.
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import { useStudioEvents } from '@/api/events'
import type { Project, Stage, StageView, Storyboard } from '@/api/types'
import { Badge, Button, Callout, Panel, PanelHeader, Spinner, cx, useToast } from '@/components/ui'

import { StageEditor } from './StageEditor'
import { TranscriptPanel } from './TranscriptPanel'

const KIND_TONE = { llm: 'info', comfy: 'ok', capture: 'muted', shot: 'warn' } as const
const KIND_LABEL = { llm: 'asks a model', comfy: 'runs a workflow', capture: 'keeps it', shot: 'builds a shot' }

type Tab = 'steps' | 'transcript'

const gigabytes = (bytes: number) => `${(bytes / 1e9).toFixed(1)} GB`

/**
 * What the language models are holding, and a way to make them let go.
 *
 * The flow alternates between a language model and ComfyUI, and on one graphics card they are competing
 * for the same memory: a 7B model resident is several gigabytes an image model cannot have. Ollama
 * releases it by itself after a few idle minutes, which is exactly the wrong amount of time when you have
 * finished writing and want to start drawing.
 *
 * It only appears when something is actually loaded, so it is an answer to a question rather than another
 * button to read past.
 */
function FreeVram() {
  const toast = useToast()
  const { data, refetch, isFetching } = useQuery({
    queryKey: ['llm-loaded'],
    queryFn: api.settings.llmLoaded,
    // Ollama unloads on its own too, so a stale "still loaded" would be a lie within a minute or two.
    refetchInterval: 30_000,
  })

  const free = useMutation({
    mutationFn: () => api.settings.llmUnload(),
    onSuccess: (result) => {
      void refetch()
      if (result.unloaded.length) toast.push('ok', `Released ${result.unloaded.join(', ')}.`)
      else if (result.warnings.length) toast.push('bad', result.warnings.join(' '))
    },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  if (!data?.models.length) return null

  return (
    <Button
      size="sm"
      variant="ghost"
      disabled={free.isPending || isFetching}
      title={
        `In memory: ${data.models.map((m) => m.name).join(', ')}.\n` +
        'Release it so ComfyUI can have the card.'
      }
      onClick={() => free.mutate()}
    >
      {free.isPending ? <Spinner /> : null} Free {gigabytes(data.vram)}
    </Button>
  )
}

export function PipelinePanel({
  project,
  board,
  onChanged,
}: {
  project: Project
  board: Storyboard
  onChanged: () => void
}) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [tab, setTab] = useState<Tab>('steps')
  const [editing, setEditing] = useState<string | null>(null)

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['board-pipeline', project.id, board.id, board.modified],
    queryFn: () => api.storyboards.pipeline(project.id, board.id),
  })
  const { data: active, refetch: refetchRun } = useQuery({
    queryKey: ['board-pipeline-run', project.id, board.id],
    queryFn: () => api.storyboards.activePipelineRun(project.id, board.id),
  })

  // The flow reports itself the same way a render does, so the panel never has to poll.
  useStudioEvents((event) => {
    if (!event.type.startsWith('storyboard.')) return
    if ((event.data as { board_id?: string }).board_id !== board.id) return
    void refetchRun()
    if (event.type === 'storyboard.stage.finished') {
      queryClient.invalidateQueries({ queryKey: ['board-transcript'] })
      queryClient.invalidateQueries({ queryKey: ['board-stills'] })
      onChanged()
    }
    if (event.type === 'storyboard.pipeline.finished') {
      const finished = event.data as { status: string; error?: string | null }
      if (finished.status === 'success') toast.push('ok', 'The flow finished.')
      else if (finished.error) toast.push('bad', finished.error)
    }
  }, [board.id])

  const save = useMutation({
    mutationFn: (stage: Stage) => api.storyboards.saveStage(project.id, board.id, stage),
    onSuccess: () => { void refetch(); onChanged(); toast.push('ok', 'Saved.') },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })
  const reset = useMutation({
    mutationFn: (stageId: string) => api.storyboards.resetStage(project.id, board.id, stageId),
    onSuccess: () => { void refetch(); onChanged() },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })
  const runOne = useMutation({
    mutationFn: (stageId: string) => api.storyboards.runStage(project.id, board.id, stageId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['board-transcript'] })
      queryClient.invalidateQueries({ queryKey: ['board-stills'] })
      onChanged()
    },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })
  const runAll = useMutation({
    mutationFn: () => api.storyboards.runPipeline(project.id, board.id, {}),
    onSuccess: () => void refetchRun(),
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  if (isLoading || !data) return <Panel><div className="p-3"><Spinner /></div></Panel>

  const stage = editing ? data.stages.find((s) => s.id === editing) : null
  const running = active?.status === 'running' ? active : null

  return (
    <Panel className="flex h-full min-h-0 flex-col overflow-hidden">
      <PanelHeader
        actions={
          !stage && (
            <div className="flex items-center gap-1">
              <FreeVram />
              {running ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={async () => {
                    await api.storyboards.cancelPipeline(project.id, board.id)
                    void refetchRun()
                  }}
                >
                  Stop
                </Button>
              ) : (
                <Button size="sm" disabled={runAll.isPending} onClick={() => runAll.mutate()}>
                  Run the flow
                </Button>
              )}
            </div>
          )
        }
      >
        <div className="flex items-center gap-1">
          {(['steps', 'transcript'] as Tab[]).map((name) => (
            <button
              key={name}
              className={cx(
                'rounded px-1.5 py-0.5 text-[11px]',
                tab === name
                  ? 'bg-[var(--color-surface-raised)] text-[var(--color-ink)]'
                  : 'text-[var(--color-ink-dim)]',
              )}
              onClick={() => { setTab(name); setEditing(null) }}
            >
              {name === 'steps' ? 'Steps' : 'What was sent'}
            </button>
          ))}
        </div>
      </PanelHeader>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === 'transcript' ? (
          <TranscriptPanel project={project} board={board} />
        ) : stage ? (
          <StageEditor
            key={stage.id}
            stage={stage}
            tokens={data.tokens}
            saving={save.isPending}
            onSave={(next) => save.mutate(next)}
            onReset={() => { reset.mutate(stage.id); setEditing(null) }}
            onClose={() => setEditing(null)}
          />
        ) : (
          <StageList
            stages={data.stages}
            running={running?.stage_id ?? null}
            done={running?.done ?? []}
            busy={runOne.isPending || Boolean(running)}
            onOpen={setEditing}
            onRun={(id) => runOne.mutate(id)}
            onToggle={(s) => save.mutate({ ...s, enabled: !s.enabled })}
          />
        )}
      </div>
    </Panel>
  )
}

function StageList({
  stages,
  running,
  done,
  busy,
  onOpen,
  onRun,
  onToggle,
}: {
  stages: StageView[]
  running: string | null
  done: string[]
  busy: boolean
  onOpen: (id: string) => void
  onRun: (id: string) => void
  onToggle: (stage: StageView) => void
}) {
  return (
    <div className="space-y-1 p-2">
      {stages.some((s) => s.stale) && (
        <Callout tone="warn">
          Some steps were edited against an older version of the defaults. Open one to see, or reset it
          to take the new wording.
        </Callout>
      )}

      {stages.map((stage, index) => (
        <div
          key={stage.id}
          className={cx(
            'rounded border px-2 py-1.5',
            running === stage.id
              ? 'border-[var(--color-accent)]'
              : 'border-[var(--color-edge)]',
            !stage.enabled && 'opacity-50',
          )}
        >
          <div className="flex items-center gap-2">
            <span className="w-4 shrink-0 text-[10px] text-[var(--color-ink-dim)]">{index + 1}</span>
            <button
              className="min-w-0 flex-1 truncate text-left text-xs font-semibold"
              onClick={() => onOpen(stage.id)}
            >
              {stage.name || stage.id}
            </button>
            {done.includes(stage.id) && <Badge tone="ok">done</Badge>}
            {running === stage.id && <Spinner />}
            {stage.edited && <Badge tone="info">edited</Badge>}
            {stage.stale && <Badge tone="warn">stale</Badge>}
            <Badge tone={KIND_TONE[stage.kind]}>{KIND_LABEL[stage.kind]}</Badge>
          </div>

          <div className="mt-0.5 flex items-center gap-2 pl-6">
            <span className="min-w-0 flex-1 truncate text-[10px] text-[var(--color-ink-dim)]">
              {stage.description}
            </span>
            <span className="shrink-0 text-[10px] text-[var(--color-ink-dim)]">
              {stage.scope === 'frame' ? 'each frame' : 'once'}
            </span>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => onRun(stage.id)}>
              Run
            </Button>
            <Button
              size="sm"
              variant="ghost"
              title={stage.enabled ? 'Skip this step' : 'Put this step back'}
              onClick={() => onToggle(stage)}
            >
              {stage.enabled ? 'Skip' : 'Use'}
            </Button>
          </div>
        </div>
      ))}
    </div>
  )
}
