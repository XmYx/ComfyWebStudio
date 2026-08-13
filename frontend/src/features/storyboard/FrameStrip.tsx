/**
 * The frames: what each one looks like so far, and the things you can do to one.
 *
 * Each action is a separate button rather than one "generate everything", because a storyboard is a thing
 * you look at and change between steps — that is the entire reason it exists rather than going straight
 * from a premise to finished shots.
 *
 *     draw  ⇄  reroll  →  look  →  make the shot
 *
 * The first two are a loop, and drawing a board is mostly spent in it: nine frames land, three are wrong,
 * you rewrite two prompts and reroll the third. So a frame shows its picture as soon as the step produces
 * one — a run's artifacts are already on disk, nothing needs keeping first — and it shows its own progress
 * while it is being drawn, because "which of these nine is happening now" is the only question worth
 * answering during a run.
 *
 * "Look" is the step people skip and shouldn't: the first image prompt was written blind from the premise,
 * and the generator then made its own choices about who is in frame and where the light falls. Describing
 * what was actually drawn is what keeps the motion prompt talking about the real picture.
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { Artifact, Project, RunStatus, Storyboard, StoryboardFrame } from '@/api/types'
import { isOurDrag, readDrag } from '@/lib/dnd'
import { useStudio } from '@/store/studio'
import {
  Badge, Button, Empty, Panel, PanelHeader, ProgressBar, Spinner, TextArea, cx, useToast,
} from '@/components/ui'

const STATUS_TONE = {
  draft: 'muted',
  imaged: 'info',
  described: 'warn',
  shot: 'ok',
} as const

/** Statuses a step does not come back from, so its share of the run is over either way. */
const FINISHED: ReadonlySet<string> = new Set<RunStatus>([
  'success', 'cached', 'error', 'cancelled', 'skipped',
])

interface Props {
  project: Project
  board: Storyboard
  onChanged: () => void
}

export function FrameStrip({ project, board, onChanged }: Props) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const liveSteps = useStudio((s) => s.liveSteps)
  const activeRun = useStudio((s) => s.activeRun)

  const fail = (error: unknown) => toast.push('bad', (error as ApiError).message)

  /** What each frame's picture is, and which step draws it. Progress arrives live, keyed by that step. */
  const { data: stills } = useQuery({
    queryKey: ['board-stills', project.id, board.id, project.modified],
    queryFn: () => api.storyboards.stills(project.id, board.id),
  })

  /**
   * Frames this session has asked to redraw.
   *
   * Until the next refetch the server still believes the kept asset is the frame's picture, while the new
   * still is already arriving over the websocket. Remembering the request is what makes a reroll appear
   * the moment it finishes rather than a request later.
   */
  const [redrawn, setRedrawn] = useState<Record<string, true>>({})

  const draw = useMutation({
    mutationFn: (body: { frame_ids?: string[]; reroll?: boolean }) =>
      api.storyboards.draw(project.id, board.id, body),
    onSuccess: (result, body) => {
      setRedrawn((current) => ({
        ...current,
        ...Object.fromEntries(Object.keys(result.steps).map((id) => [id, true as const])),
      }))
      const count = Object.keys(result.steps).length
      if (body.reroll && !result.seeded) {
        toast.push(
          'info',
          `${result.workflow} exposes no seed, so this was re-rendered rather than varied — it may come ` +
            'back looking the same. Expose the sampler\'s seed in ComfyUI to reroll properly.',
        )
      } else {
        toast.push('ok', `Drawing ${count} frame${count === 1 ? '' : 's'} with ${result.workflow}…`)
      }
      queryClient.invalidateQueries({ queryKey: ['board-stills', project.id, board.id] })
      queryClient.invalidateQueries({ queryKey: ['project', project.id] })
    },
    onError: fail,
  })

  const act = async (frame: StoryboardFrame, what: 'capture' | 'describe' | 'shot') => {
    try {
      if (what === 'capture') await api.storyboards.captureFrame(project.id, board.id, frame.id)
      if (what === 'describe') await api.storyboards.describeFrame(project.id, board.id, frame.id)
      if (what === 'shot') {
        const shot = await api.storyboards.buildShot(project.id, board.id, frame.id)
        toast.push('ok', `Made “${shot.name}”.`)
      }
      queryClient.invalidateQueries({ queryKey: ['board-stills', project.id, board.id] })
      onChanged()
    } catch (error) {
      fail(error)
    }
  }

  const patch = async (frame: StoryboardFrame, body: Partial<StoryboardFrame>) => {
    try {
      await api.storyboards.updateFrame(project.id, board.id, frame.id, body)
      queryClient.invalidateQueries({ queryKey: ['board-stills', project.id, board.id] })
      onChanged()
    } catch (error) {
      fail(error)
    }
  }

  /** Point a frame at an image the project already has, instead of one it drew. */
  const useAsset = (frame: StoryboardFrame, assetId: string) => {
    // The dropped picture is the frame's picture now, so stop preferring what its step last drew.
    setRedrawn(({ [frame.id]: _dropped, ...rest }) => rest)
    patch(frame, { asset_id: assetId })
  }

  const characterName = (id: string) =>
    board.characters.find((c) => c.id === id)?.name ?? 'someone'

  /**
   * Whether this frame's shot still exists.
   *
   * Not just `frame.shot_id`: deleting the shot used to leave the id pointing at nothing, and the frame
   * then reported itself finished and refused to build another one — permanently. Deleting a shot now
   * clears the link, and checking the shot is really there covers every other way one might vanish, an
   * imported project included.
   */
  const madeShot = (frame: StoryboardFrame) =>
    Boolean(frame.shot_id) && project.shots.some((shot) => shot.id === frame.shot_id)

  /** The picture a frame is showing, and how it is getting on if it is being drawn right now. */
  const stateOf = (frame: StoryboardFrame) => {
    const still = stills?.frames[frame.id]
    const live = still?.step_id ? liveSteps[still.step_id] : undefined
    // A hand-picked asset wins over the step's last output — unless this session just asked to redraw.
    const preferDrawn = still?.source !== 'asset' || redrawn[frame.id]
    const output: Artifact | undefined = preferDrawn
      ? live?.outputs?.find((a) => a.kind === 'image')
      : undefined
    const path = output?.thumb ?? output?.path ?? still?.thumb ?? still?.image ?? null
    const status = live?.status
    return {
      still,
      path: path ? api.media.url(project.id, path) : null,
      busy: status === 'running' || status === 'pending',
      progress: live?.progress ?? 0,
      error: status === 'error' ? live?.error ?? 'This frame failed to draw.' : null,
      cached: status === 'cached',
    }
  }

  // Progress for the whole board, which is the run that is drawing it. Steps rather than frames, because
  // a run is what has steps — and a partial redraw of three frames is still a run worth a bar.
  const drawing =
    activeRun?.status === 'running' && stills?.shot_id && activeRun.shot_id === stills.shot_id
      ? activeRun
      : null
  const runSteps = drawing?.step_runs.map((sr) => sr.step_id) ?? []
  const finished = runSteps.filter((id) => FINISHED.has(liveSteps[id]?.status ?? '')).length
  const runProgress = runSteps.length
    ? runSteps.reduce(
        (sum, id) =>
          sum + (FINISHED.has(liveSteps[id]?.status ?? '') ? 1 : liveSteps[id]?.progress ?? 0),
        0,
      ) / runSteps.length
    : 0
  const drawnCount = board.frames.filter((frame) => stateOf(frame).path).length
  const busyElsewhere = Boolean(activeRun && activeRun.status === 'running' && !drawing)

  return (
    <Panel className="flex h-full min-h-0 flex-col overflow-hidden">
      <PanelHeader
        actions={
          <>
            <Button
              size="sm"
              disabled={!board.frames.length || draw.isPending || Boolean(drawing) || busyElsewhere}
              title="Give every frame a step and draw them all"
              onClick={() => draw.mutate({})}
            >
              {draw.isPending ? <Spinner /> : null} Draw all
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={async () => {
                await api.storyboards.addFrame(project.id, board.id)
                onChanged()
              }}
            >
              + Frame
            </Button>
          </>
        }
      >
        {board.frames.length} frame{board.frames.length === 1 ? '' : 's'}
        {board.frames.length > 0 && (
          <span className="ml-2 text-[10px] text-[var(--color-ink-dim)]">{drawnCount} drawn</span>
        )}
      </PanelHeader>

      {drawing && (
        <div className="flex items-center gap-2 border-b border-[var(--color-edge)] bg-[var(--color-surface)] px-2 py-1.5">
          <span className="text-[10px] tabular-nums text-[var(--color-ink-dim)]">
            {finished} / {runSteps.length}
          </span>
          <div className="min-w-0 flex-1">
            <ProgressBar value={runProgress} />
          </div>
          <span className="w-8 text-right text-[10px] tabular-nums text-[var(--color-ink-dim)]">
            {Math.round(runProgress * 100)}%
          </span>
          <Button
            size="sm"
            variant="ghost"
            title="Stop drawing"
            onClick={() =>
              api.runs.cancel(project.id, drawing.id).then((r) => toast.push('info', r.message), fail)
            }
          >
            Stop
          </Button>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {!board.frames.length ? (
          <Empty title="No frames yet">
            Write a premise on the left, then press <strong>Write the shots</strong>.
          </Empty>
        ) : (
          board.frames.map((frame) => {
            const state = stateOf(frame)
            return (
              <div
                key={frame.id}
                data-frame={frame.id}
                className={cx(
                  'mb-2 rounded-md border bg-[var(--color-surface)] p-2',
                  state.error ? 'border-[var(--color-bad)]' : 'border-[var(--color-edge)]',
                  state.busy && 'border-[var(--color-accent)]',
                )}
              >
                <div className="mb-1.5 flex items-center gap-2">
                  <span className="text-[10px] text-[var(--color-ink-dim)]">{frame.order + 1}</span>
                  <span className="min-w-0 flex-1 truncate text-xs font-semibold">
                    {frame.title || 'Untitled'}
                  </span>
                  {state.busy && <Badge tone="info">drawing</Badge>}
                  {state.cached && !state.busy && <Badge tone="muted">cached</Badge>}
                  <Badge tone={STATUS_TONE[frame.status]}>{frame.status}</Badge>
                  <button
                    title="Remove this frame"
                    className="px-1 text-[10px] text-[var(--color-ink-dim)] hover:text-[var(--color-bad)]"
                    onClick={async () => {
                      await api.storyboards.removeFrame(project.id, board.id, frame.id)
                      onChanged()
                    }}
                  >
                    ✕
                  </button>
                </div>

                <div className="flex gap-2">
                  {/* Also a drop target: a photograph or a plate is a perfectly good starting image. */}
                  <div
                    className={cx(
                      'relative h-20 w-32 shrink-0 overflow-hidden rounded border',
                      state.path
                        ? 'border-[var(--color-edge)]'
                        : 'border-dashed border-[var(--color-edge)]',
                    )}
                    title="Drop an image here to use it as this frame's starting image"
                    onDragOver={(event) => {
                      if (!isOurDrag(event)) return
                      event.preventDefault()
                      event.dataTransfer.dropEffect = 'copy'
                    }}
                    onDrop={(event) => {
                      const payload = readDrag(event)
                      if (payload?.kind !== 'asset' || payload.mediaKind !== 'image') return
                      event.preventDefault()
                      useAsset(frame, payload.id)
                    }}
                  >
                    {state.path ? (
                      <img src={state.path} alt="" className="size-full object-cover" />
                    ) : (
                      <div className="grid size-full place-items-center text-[10px] text-[var(--color-ink-dim)]">
                        not drawn
                      </div>
                    )}
                    {state.still?.source === 'asset' && !state.busy && (
                      <span className="absolute left-1 top-1 rounded bg-black/60 px-1 text-[9px] text-white">
                        kept
                      </span>
                    )}
                    {state.busy && (
                      <>
                        <div className="absolute inset-0 grid place-items-center bg-black/50 text-[10px] tabular-nums text-white">
                          {Math.round(state.progress * 100)}%
                        </div>
                        <div className="absolute inset-x-0 bottom-0">
                          <ProgressBar value={state.progress} />
                        </div>
                      </>
                    )}
                  </div>

                  <div className="min-w-0 flex-1 space-y-1">
                    {frame.camera && (
                      <div className="text-[10px] italic text-[var(--color-ink-dim)]">{frame.camera}</div>
                    )}
                    <TextArea
                      rows={2}
                      data-field="image_prompt"
                      defaultValue={frame.image_prompt}
                      placeholder="what the still looks like"
                      className="text-[11px]"
                      onBlur={(e) =>
                        e.target.value !== frame.image_prompt &&
                        patch(frame, { image_prompt: e.target.value })
                      }
                    />
                    <TextArea
                      rows={2}
                      data-field="shot_prompt"
                      defaultValue={frame.shot_prompt}
                      placeholder="how it moves"
                      className="text-[11px]"
                      onBlur={(e) =>
                        e.target.value !== frame.shot_prompt &&
                        patch(frame, { shot_prompt: e.target.value })
                      }
                    />
                    {frame.character_ids.length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {frame.character_ids.map((id) => (
                          <Badge key={id} tone="muted">{characterName(id)}</Badge>
                        ))}
                      </div>
                    )}
                  </div>
                </div>

                {state.error && (
                  <div className="mt-1.5 text-[10px] leading-relaxed text-[var(--color-bad)]">
                    {state.error}
                  </div>
                )}

                <div className="mt-1.5 flex flex-wrap items-center gap-1">
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={state.busy || draw.isPending || busyElsewhere}
                    title={
                      state.path
                        ? 'Draw this frame again from its prompt'
                        : 'Draw this frame from its prompt'
                    }
                    onClick={() => draw.mutate({ frame_ids: [frame.id] })}
                  >
                    {state.path ? 'Redraw' : 'Draw'}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={state.busy || draw.isPending || busyElsewhere}
                    title="Draw this frame again with a new seed — the same prompt, a different picture"
                    onClick={() => draw.mutate({ frame_ids: [frame.id], reroll: true })}
                  >
                    ↻ Vary
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={!state.path || state.still?.kept}
                    title={
                      state.still?.kept
                        ? 'This picture is already a project asset'
                        : 'Keep this still as a project asset'
                    }
                    onClick={() => act(frame, 'capture')}
                  >
                    Keep
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={!state.path}
                    title="Look at what was drawn and rewrite the prompts from it"
                    onClick={() => act(frame, 'describe')}
                  >
                    Look
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={madeShot(frame) || !state.path}
                    title={
                      madeShot(frame)
                        ? 'This frame already has a shot'
                        : 'Turn this frame into a shot, using the picture shown'
                    }
                    onClick={() => act(frame, 'shot')}
                  >
                    {madeShot(frame) ? 'Shot made' : 'Make the shot'}
                  </Button>
                  {frame.action && (
                    <span className={cx('ml-auto truncate text-[10px] text-[var(--color-ink-dim)]')}>
                      {frame.action.slice(0, 70)}
                    </span>
                  )}
                </div>
              </div>
            )
          })
        )}
      </div>
    </Panel>
  )
}
