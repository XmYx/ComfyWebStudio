/**
 * The shots panel: what exists, what it is doing, and what to render next.
 *
 * Two jobs that pull in different directions, so they are kept apart. *Which shot the canvas shows* is one
 * click on the name. *Which shots the next render covers* is a set of tick boxes. Making one gesture do
 * both would mean you could not look at a shot without queueing it, which is the wrong default for the
 * thing you do most.
 *
 * Progress is computed here from the live step map rather than fetched: a shot's own steps are already
 * arriving on the websocket, so a bar per shot costs nothing and never lags behind the canvas.
 */

import { useMemo } from 'react'
import type { Run, Shot } from '@/api/types'
import { useStudio, type LiveStep } from '@/store/studio'
import { startDrag } from '@/lib/dnd'
import { Badge, Button, Panel, PanelHeader, ProgressBar, Spinner, cx } from '@/components/ui'

/** Statuses a step no longer moves out of, so it counts as a whole step done. */
const TERMINAL = new Set(['success', 'cached', 'error', 'cancelled', 'skipped'])

interface Props {
  shots: Shot[]
  currentId: string | null
  onOpen: (shotId: string) => void
  onContextMenu: (event: React.MouseEvent, shot: Shot) => void
  onCreate: () => void
  onRender: (shotIds: string[]) => void
  onStop: () => void
}

export function ShotList({
  shots, currentId, onOpen, onContextMenu, onCreate, onRender, onStop,
}: Props) {
  const selected = useStudio((s) => s.selectedShotIds)
  const setSelected = useStudio((s) => s.setSelectedShots)
  const shotRuns = useStudio((s) => s.shotRuns)
  const liveSteps = useStudio((s) => s.liveSteps)
  const queue = useStudio((s) => s.queue)

  const selectedSet = useMemo(() => new Set(selected), [selected])
  // Only the ticked shots that still exist, in panel order: deleting a ticked shot must not render a
  // ghost, and "render selected" should follow the list the user is looking at.
  const toRender = useMemo(
    () => shots.filter((s) => selectedSet.has(s.id)).map((s) => s.id),
    [shots, selectedSet],
  )

  const click = (event: React.MouseEvent, shot: Shot, index: number) => {
    if (event.shiftKey && currentId) {
      const from = shots.findIndex((s) => s.id === currentId)
      if (from >= 0) {
        const [low, high] = from < index ? [from, index] : [index, from]
        setSelected(shots.slice(low, high + 1).map((s) => s.id))
        return
      }
    }
    if (event.ctrlKey || event.metaKey) {
      toggle(shot.id)
      return
    }
    // A plain click is about looking, not about queueing — it opens the shot and drops any tick marks,
    // which is also the only way to clear a selection without hunting for a "clear" button.
    setSelected([])
    onOpen(shot.id)
  }

  const toggle = (shotId: string) =>
    setSelected(
      selectedSet.has(shotId) ? selected.filter((id) => id !== shotId) : [...selected, shotId],
    )

  const busy = Boolean(queue)
  const allTicked = shots.length > 0 && toRender.length === shots.length

  return (
    <Panel>
      <PanelHeader
        actions={
          <>
            {busy ? (
              <Button size="sm" variant="danger" onClick={onStop} title="Stop the render queue">
                <Spinner /> Stop
              </Button>
            ) : (
              <Button
                size="sm"
                variant="primary"
                disabled={!shots.length}
                title={
                  toRender.length
                    ? 'Render the ticked shots, one after another'
                    : 'Render every shot, one after another'
                }
                onClick={() => onRender(toRender)}
              >
                ▶ {toRender.length ? `Render ${toRender.length}` : 'Render all'}
              </Button>
            )}
            <Button size="sm" onClick={onCreate} title="New shot">+</Button>
          </>
        }
      >
        Shots
      </PanelHeader>

      {shots.length > 1 && (
        <div className="flex items-center gap-2 border-b border-[var(--color-edge)] px-2 py-1 text-[10px] text-[var(--color-ink-dim)]">
          <input
            type="checkbox"
            checked={allTicked}
            // Indeterminate is the honest picture of a partial selection, and it is only reachable
            // through the DOM node itself.
            ref={(node) => {
              if (node) node.indeterminate = toRender.length > 0 && !allTicked
            }}
            onChange={() => setSelected(allTicked ? [] : shots.map((s) => s.id))}
            aria-label="Select every shot"
            className="size-3 accent-[var(--color-accent)]"
          />
          <span>
            {toRender.length ? `${toRender.length} of ${shots.length} selected` : 'Select shots to render'}
          </span>
          {queue && (
            <span className="ml-auto">
              {Object.keys(queue.done).length}/{queue.shotIds.length} rendered
            </span>
          )}
        </div>
      )}

      <div className="max-h-48 overflow-y-auto p-1">
        {!shots.length ? (
          <div className="p-2 text-xs text-[var(--color-ink-dim)]">Create a shot to begin.</div>
        ) : (
          shots.map((shot, index) => (
            <Row
              key={shot.id}
              shot={shot}
              index={index}
              open={shot.id === currentId}
              ticked={selectedSet.has(shot.id)}
              // "Queued" has to beat a stale ✓ from an earlier render, so it is decided by the shot's
              // place in the queue rather than by its last run — but only for shots the queue has not
              // reached yet. Without excluding the current one, a shot that has just finished reads as
              // queued again for the few milliseconds before the batch gets round to announcing it.
              queued={Boolean(
                queue &&
                  queue.shotIds.includes(shot.id) &&
                  !queue.done[shot.id] &&
                  queue.current !== shot.id,
              )}
              current={queue?.current === shot.id}
              run={shotRuns[shot.id]}
              liveSteps={liveSteps}
              onTick={() => toggle(shot.id)}
              onClick={click}
              onContextMenu={onContextMenu}
            />
          ))
        )}
      </div>
    </Panel>
  )
}

function Row({
  shot, index, open, current, ticked, queued, run, liveSteps, onTick, onClick, onContextMenu,
}: {
  shot: Shot
  index: number
  /** The shot the canvas is showing. */
  open: boolean
  /** The shot the queue is on, which is true from the moment it is picked up rather than from its
   *  first step — the gap between the two is small but it is not nothing. */
  current: boolean
  ticked: boolean
  queued: boolean
  run: { runId: string; stepIds: string[]; status: Run['status']; error?: string | null } | undefined
  liveSteps: Record<string, LiveStep>
  onTick: () => void
  onClick: (event: React.MouseEvent, shot: Shot, index: number) => void
  onContextMenu: (event: React.MouseEvent, shot: Shot) => void
}) {
  const running = current || run?.status === 'running'
  const progress = useProgress(run?.stepIds, liveSteps, running)
  const pieces = shot.steps.length + shot.instances.length

  return (
    <div
      onContextMenu={(event) => onContextMenu(event, shot)}
      className={cx(
        'rounded px-1 transition-colors',
        open ? 'bg-[var(--color-panel-2)]' : 'hover:bg-[var(--color-panel-2)]/60',
      )}
    >
      <div className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={ticked}
          onChange={onTick}
          // The row's own click opens the shot; ticking is a different intent and must not do both.
          onClick={(event) => event.stopPropagation()}
          aria-label={`Select ${shot.name} for rendering`}
          className="size-3 shrink-0 accent-[var(--color-accent)]"
        />
        <button
          draggable
          // Dragging a shot onto another shot's canvas makes a node supplying its last result.
          onDragStart={(event) => startDrag(event, { kind: 'shot', id: shot.id, name: shot.name })}
          title="Click to open, ctrl-click to add to the selection, or drag onto a canvas to use its output"
          onClick={(event) => onClick(event, shot, index)}
          className={cx(
            'flex min-w-0 flex-1 items-center justify-between gap-2 py-1.5 text-left text-xs',
            open ? 'text-[var(--color-ink)]' : 'text-[var(--color-ink-dim)]',
          )}
        >
          <span className="truncate">{shot.name}</span>
          <span className="flex shrink-0 items-center gap-1.5">
            <Status status={run?.status} running={running} queued={queued} error={run?.error} />
            {/* Placed templates count too — a shot made only of them is not an empty shot. */}
            <span className="text-[10px]">{pieces}</span>
          </span>
        </button>
      </div>

      {running && (
        <div className="pb-1 pl-5 pr-1">
          <ProgressBar value={progress} />
        </div>
      )}
    </div>
  )
}

/**
 * How far through its steps this shot is.
 *
 * A step that has finished counts whole however it finished — a failed step is not going to make any more
 * progress, and a bar that stalls at 60% because one step died says less than one that fills and a status
 * that says "failed".
 */
function useProgress(
  stepIds: string[] | undefined,
  liveSteps: Record<string, LiveStep>,
  running: boolean,
): number {
  return useMemo(() => {
    if (!running || !stepIds?.length) return 0
    const total = stepIds.reduce((sum, id) => {
      const live = liveSteps[id]
      if (!live) return sum
      return sum + (TERMINAL.has(live.status) ? 1 : live.progress || 0)
    }, 0)
    return Math.min(1, total / stepIds.length)
  }, [stepIds, liveSteps, running])
}

function Status({
  status, running, queued, error,
}: { status?: Run['status']; running: boolean; queued: boolean; error?: string | null }) {
  if (running) return <Spinner />
  if (queued) return <Badge tone="muted" title="Waiting its turn in the queue">queued</Badge>
  switch (status) {
    case 'success':
    case 'cached':
      return <span title="Rendered" className="text-[10px] text-[var(--color-ok)]">✓</span>
    case 'error':
      return (
        <span title={error || 'This shot failed'} className="text-[10px] text-[var(--color-bad)]">!</span>
      )
    case 'cancelled':
      return <span title="Cancelled" className="text-[10px] text-[var(--color-ink-dim)]">–</span>
    default:
      return null
  }
}
