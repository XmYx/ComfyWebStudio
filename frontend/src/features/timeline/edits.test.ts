import { describe, expect, it } from 'vitest'

import { travelOrder } from './edits'

const clips = [
  { id: 'a', start: 0 },
  { id: 'b', start: 1 },
  { id: 'c', start: 2 },
]

describe('the order a multi-clip move is sent in', () => {
  it('leads with the rightmost when moving right', () => {
    expect(travelOrder(clips, +1).map((c) => c.id)).toEqual(['c', 'b', 'a'])
  })

  it('leads with the leftmost when moving left', () => {
    expect(travelOrder(clips, -1).map((c) => c.id)).toEqual(['a', 'b', 'c'])
  })

  it('treats a pure resize as moving right, which is the direction its end travels', () => {
    expect(travelOrder(clips, 0).map((c) => c.id)).toEqual(['c', 'b', 'a'])
  })

  it('leaves the caller its own list', () => {
    const original = [...clips]
    travelOrder(clips, 1)
    expect(clips).toEqual(original)
  })

  it('never puts a clip before one it is about to land on', () => {
    // Two clips butted together, moved right by exactly one length: 'a' lands where 'b' is now, so 'b'
    // has to have been told to move first or the server will trim it out of existence on the way past.
    const adjacent = [{ id: 'a', start: 0 }, { id: 'b', start: 1 }]
    expect(travelOrder(adjacent, +1).map((c) => c.id)).toEqual(['b', 'a'])
  })
})
