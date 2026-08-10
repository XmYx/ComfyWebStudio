/**
 * Dragging library entries onto the shot canvas.
 *
 * One small payload type shared by every draggable thing, carried on a custom MIME type rather than
 * `text/plain`: the browser hands `text/plain` to any drop target on the page, and a shot id landing in a
 * prompt field because someone missed the canvas is a bad afternoon.
 */

export const DND_MIME = 'application/x-comfywebstudio'

export type DragPayload =
  | { kind: 'workflow'; id: string; name: string }
  | { kind: 'shot'; id: string; name: string }
  | { kind: 'asset'; id: string; name: string; mediaKind: string }
  | { kind: 'template'; id: string; name: string }

/** Attach a payload to a drag. Also sets a plain-text fallback so dragging out of the app reads sensibly. */
export function startDrag(event: React.DragEvent, payload: DragPayload): void {
  event.dataTransfer.setData(DND_MIME, JSON.stringify(payload))
  event.dataTransfer.setData('text/plain', payload.name)
  event.dataTransfer.effectAllowed = 'copy'
}

/** The payload a drop carries, or null when it is something else entirely. */
export function readDrag(event: React.DragEvent): DragPayload | null {
  const raw = event.dataTransfer.getData(DND_MIME)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed.kind === 'string' ? (parsed as DragPayload) : null
  } catch {
    return null
  }
}

/** True when this drag is one of ours, checked without reading the data (which is denied during dragover). */
export function isOurDrag(event: React.DragEvent): boolean {
  return Array.from(event.dataTransfer.types).includes(DND_MIME)
}
