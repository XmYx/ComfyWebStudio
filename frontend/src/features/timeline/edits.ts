/**
 * Rules for changing several clips at once.
 *
 * Small enough to be obvious and important enough to be wrong in a way nobody would notice until a clip
 * had vanished, so it lives here with a test rather than inline in a drag handler.
 */

export interface Travelling {
  id: string
  start: number
}

/**
 * The order to send a multi-clip move in: the leading clip first.
 *
 * The server makes room for a clip by trimming whatever it lands on. So when two selected clips are
 * moved to the right, patching the trailing one first would drop it onto the leading one's *old*
 * position — which is still occupied, because that clip has not been told to move yet. The server would
 * dutifully trim it away, and the patch that was about to rescue it would then have nothing to address.
 *
 * Moving right, the rightmost goes first; moving left, the leftmost does. Either way each clip lands on
 * ground its neighbour has already left.
 */
export function travelOrder<T extends Travelling>(items: T[], delta: number): T[] {
  return [...items].sort((a, b) => (delta >= 0 ? b.start - a.start : a.start - b.start))
}
