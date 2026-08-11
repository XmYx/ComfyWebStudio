/**
 * Snapping on the timeline.
 *
 * Cuts are made against other cuts: a clip should butt up against its neighbour exactly, not land a
 * hundredth of a second short and leave a black frame nobody notices until the render. So a dragged edge
 * is pulled to the interesting times near it — the ends of other clips, the playhead, the start.
 *
 * Two rules keep it from becoming a nuisance. It works in *pixels*, not seconds, so zooming in makes it
 * finer rather than fighting you; and it is always overridable, because the one time you want 0.03s of
 * overlap you really do want it.
 */

import type { Timeline } from '@/api/types'

/** How close, on screen, an edge has to be before it is pulled in. */
export const SNAP_PX = 8

export interface SnapTarget {
  time: number
  /** What it is, for the guide's tooltip — 'clip' | 'playhead' | 'start'. */
  kind: string
}

export interface SnapResult {
  /** The time to use, snapped or not. */
  time: number
  /** The target it landed on, or null when nothing was near. */
  target: SnapTarget | null
}

/**
 * Every time worth snapping to.
 *
 * `exceptClipId` leaves out the clip being dragged: its own edges are always exactly where it is, so
 * without this a clip would snap to itself and never move.
 */
export function snapTargets(
  timeline: Timeline, playhead: number, exceptClipId?: string,
): SnapTarget[] {
  const targets: SnapTarget[] = [{ time: 0, kind: 'start' }, { time: playhead, kind: 'playhead' }]
  for (const track of timeline.tracks) {
    for (const clip of track.clips) {
      if (clip.id === exceptClipId) continue
      targets.push({ time: clip.start, kind: 'clip' }, { time: clip.start + clip.duration, kind: 'clip' })
    }
  }
  return targets
}

/**
 * Pull `time` to the nearest target within the threshold.
 *
 * `zoom` is pixels per second, which is what makes the threshold a constant on screen rather than a
 * constant in seconds — the whole point of snapping to what you can see.
 */
export function snap(
  time: number, targets: SnapTarget[], zoom: number, enabled = true,
): SnapResult {
  if (!enabled || zoom <= 0) return { time, target: null }

  let best: SnapTarget | null = null
  let bestDistance = SNAP_PX
  for (const target of targets) {
    const distance = Math.abs(target.time - time) * zoom
    if (distance <= bestDistance) {
      best = target
      bestDistance = distance
    }
  }
  return best ? { time: best.time, target: best } : { time, target: null }
}

/**
 * Snap a clip being *moved*, considering both of its edges.
 *
 * Only the leading edge is not enough: dragging a clip so its *end* meets the next one's start is just as
 * common as lining up its beginning, and an editor that only did the former would feel half-finished.
 */
export function snapMove(
  start: number, duration: number, targets: SnapTarget[], zoom: number, enabled = true,
): SnapResult {
  const head = snap(start, targets, zoom, enabled)
  const tail = snap(start + duration, targets, zoom, enabled)

  if (head.target && tail.target) {
    // Both are in range; take whichever is actually closer.
    const headDistance = Math.abs(head.time - start)
    const tailDistance = Math.abs(tail.time - (start + duration))
    return headDistance <= tailDistance
      ? head
      : { time: tail.time - duration, target: tail.target }
  }
  if (head.target) return head
  if (tail.target) return { time: tail.time - duration, target: tail.target }
  return { time: start, target: null }
}
