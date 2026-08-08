import type { PortKind } from '@/api/types'

export const KIND_COLOR: Record<string, string> = {
  image: 'var(--kind-image)',
  mask: 'var(--kind-mask)',
  video: 'var(--kind-video)',
  audio: 'var(--kind-audio)',
  latent: 'var(--kind-latent)',
  string: 'var(--kind-string)',
  int: 'var(--kind-int)',
  float: 'var(--kind-float)',
  boolean: 'var(--kind-boolean)',
  file: 'var(--kind-file)',
}

export const KIND_ICON: Record<string, string> = {
  image: '🖼', mask: '◐', video: '🎞', audio: '🔊', latent: '⧉',
  string: 'T', int: '#', float: '#', boolean: '◇', file: '📄',
}

/**
 * Mirrors `can_connect` in backend/comfywebstudio/core/models.py.
 *
 * Duplicated deliberately: the canvas has to reject an impossible connection while the user is still
 * dragging it, which cannot wait for a round trip. The server re-checks on create, so this being
 * momentarily out of date can only ever be cosmetic.
 */
const IMPLICIT: Record<string, string[]> = {
  image: ['mask'],
  mask: ['image'],
  float: ['int'],
  string: ['int', 'float', 'boolean'],
  file: ['image', 'video', 'audio', 'latent', 'mask'],
}

export function canConnect(from: PortKind, to: PortKind): boolean {
  return from === to || (IMPLICIT[to]?.includes(from) ?? false)
}

export function isScalar(kind: PortKind): boolean {
  return kind === 'string' || kind === 'int' || kind === 'float' || kind === 'boolean'
}

export function isVisual(kind: PortKind): boolean {
  return kind === 'image' || kind === 'mask' || kind === 'video'
}
