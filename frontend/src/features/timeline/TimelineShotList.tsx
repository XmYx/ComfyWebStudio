/**
 * The shots, as sources to drag onto the timeline.
 *
 * The shot list in the editor is for *opening* a shot; here it is a bin — what each shot produced, ready
 * to be placed. So it says whether a shot has actually rendered anything, because a shot that has not run
 * has nothing to cut with, and finding that out by dragging it and being refused is a poor way to learn.
 */

import { api, ApiError } from '@/api/client'
import type { Project } from '@/api/types'
import { startDrag } from '@/lib/dnd'
import { Badge, Button, Empty, Panel, PanelHeader, useToast, cx } from '@/components/ui'

export function TimelineShotList({
  project, onChanged,
}: { project: Project; onChanged: () => void }) {
  const toast = useToast()

  // Which shots are already cut in, read from the timeline itself rather than from the resolved clips —
  // the resolution only reports what a clip points at, not which shot it came from.
  const used = new Set(
    project.timeline.tracks.flatMap((track) =>
      track.clips.map((clip) => clip.source.shot_id).filter(Boolean),
    ),
  )

  const shots = project.shots.filter((shot) => !shot.template_edit_id)

  // A shot can be cut in before it has run — the clip fills itself in later — so this is a note about
  // what to expect on the timeline, not a reason it cannot be placed.
  const hasRun = (shotId: string) =>
    Object.values(project.assets).some((asset) => asset.source?.shot_id === shotId)

  const place = async (shotId: string) => {
    try {
      await api.timeline.fromShot(project.id, { shot_id: shotId })
      onChanged()
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    }
  }

  return (
    <Panel className="flex h-full min-h-0 flex-col overflow-hidden">
      <PanelHeader>Shots</PanelHeader>
      <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
        {!shots.length ? (
          <Empty title="No shots yet">Build a shot first, then drag it in here.</Empty>
        ) : (
          shots.map((shot) => (
            <div
              key={shot.id}
              draggable
              onDragStart={(event) => startDrag(event, { kind: 'shot', id: shot.id, name: shot.name })}
              onDoubleClick={() => void place(shot.id)}
              title={
                hasRun(shot.id)
                  ? 'Drag onto the timeline, or double-click to append it'
                  : 'Not run yet — place it anyway and it will fill in once it has'
              }
              className={cx(
                'group mb-1 flex cursor-grab items-center gap-2 rounded-md border px-2 py-1.5',
                'border-[var(--color-edge)] bg-[var(--color-surface)] active:cursor-grabbing',
                'hover:border-[var(--color-accent)]/60',
              )}
            >
              <span className="min-w-0 flex-1 truncate text-xs">{shot.name}</span>
              {!hasRun(shot.id) && <Badge tone="warn">not run</Badge>}
              {used.has(shot.id) && <Badge tone="muted">on timeline</Badge>}
              <span className="text-[10px] text-[var(--color-ink-dim)]">
                {shot.steps.length + shot.instances.length}
              </span>
              <Button
                size="sm"
                variant="ghost"
                title="Append this shot to the timeline"
                onClick={(event) => { event.stopPropagation(); void place(shot.id) }}
              >
                +
              </Button>
            </div>
          ))
        )}
      </div>
    </Panel>
  )
}
