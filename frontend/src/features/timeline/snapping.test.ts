import { describe, expect, it } from 'vitest'

import { SNAP_PX, snap, snapMove, snapTargets } from './snapping'
import type { Timeline } from '@/api/types'

const clip = (id: string, start: number, duration: number) =>
  ({ id, start, duration }) as Timeline['tracks'][number]['clips'][number]

const timeline = (...clips: Array<ReturnType<typeof clip>>): Timeline =>
  ({ tracks: [{ id: 't', kind: 'video', clips }] }) as unknown as Timeline

describe('what there is to snap to', () => {
  it('offers both ends of every clip', () => {
    const targets = snapTargets(timeline(clip('a', 2, 3)), 0)
    expect(targets.filter((t) => t.kind === 'clip').map((t) => t.time)).toEqual([2, 5])
  })

  it('offers the playhead and the start', () => {
    const kinds = snapTargets(timeline(), 7).map((t) => `${t.kind}@${t.time}`)
    expect(kinds).toEqual(['start@0', 'playhead@7'])
  })

  it('leaves out the clip being dragged, which would otherwise snap to itself', () => {
    const targets = snapTargets(timeline(clip('a', 2, 3), clip('b', 9, 1)), 0, 'a')
    expect(targets.filter((t) => t.kind === 'clip').map((t) => t.time)).toEqual([9, 10])
  })
})

describe('snapping a single edge', () => {
  const targets = [{ time: 5, kind: 'clip' }]

  it('pulls to a target within the threshold', () => {
    // 40 px per second, so 0.1s away is 4 px — inside the 8 px reach.
    expect(snap(5.1, targets, 40).time).toBe(5)
  })

  it('leaves it alone when nothing is near', () => {
    expect(snap(8, targets, 40)).toEqual({ time: 8, target: null })
  })

  it('reports what it landed on, so a guide can be drawn', () => {
    expect(snap(5.1, targets, 40).target).toEqual({ time: 5, kind: 'clip' })
  })

  it('is a distance on screen, not in seconds', () => {
    // The same 0.1s is 4 px at 40 px/s but 40 px when zoomed to 400 — out of reach there.
    expect(snap(5.1, targets, 40).target).not.toBeNull()
    expect(snap(5.1, targets, 400).target).toBeNull()
  })

  it('takes the nearest of several', () => {
    const near = [{ time: 5, kind: 'clip' }, { time: 5.15, kind: 'playhead' }]
    expect(snap(5.12, near, 40).time).toBe(5.15)
  })

  it('does nothing when it is switched off', () => {
    expect(snap(5.1, targets, 40, false)).toEqual({ time: 5.1, target: null })
  })

  it('reaches exactly as far as the threshold says', () => {
    const zoom = 40
    const justInside = 5 + (SNAP_PX / zoom) * 0.99
    const justOutside = 5 + (SNAP_PX / zoom) * 1.01
    expect(snap(justInside, targets, zoom).target).not.toBeNull()
    expect(snap(justOutside, targets, zoom).target).toBeNull()
  })
})

describe('snapping a clip being moved', () => {
  const targets = [{ time: 10, kind: 'clip' }]

  it('lines up its leading edge', () => {
    expect(snapMove(9.9, 2, targets, 40).time).toBe(10)
  })

  it('also lines up its trailing edge, so a clip can be butted up to the next one', () => {
    // Its end (8 + 2 = 10) is on the target; the clip therefore starts at 8.
    expect(snapMove(8.05, 2, targets, 40).time).toBeCloseTo(8)
  })

  it('prefers whichever edge is closer', () => {
    const both = [{ time: 10, kind: 'clip' }, { time: 12.2, kind: 'clip' }]
    // Head is 0.1 from 10; tail (10.1 + 2 = 12.1) is 0.1 from 12.2 — a tie broken towards the head.
    expect(snapMove(10.1, 2, both, 40).time).toBe(10)
  })

  it('leaves it where it is when neither edge is near anything', () => {
    expect(snapMove(3, 2, targets, 40)).toEqual({ time: 3, target: null })
  })
})
