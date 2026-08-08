/**
 * Artifact previews.
 *
 * A registry keyed by port kind, so adding a kind means adding one renderer. Everything is served through
 * our own /media endpoint rather than ComfyUI's /view — ComfyUI's origin middleware would reject a
 * cross-origin browser request anyway.
 */

import { useState, type ReactNode } from 'react'
import { api } from '@/api/client'
import type { Artifact } from '@/api/types'
import { KIND_COLOR, KIND_ICON } from '@/lib/kinds'
import { Badge, Empty, cx } from '@/components/ui'
import { formatBytes } from '@/lib/format'

interface RendererProps {
  projectId: string
  artifact: Artifact
  /** Several artifacts sharing a port key form a sequence (a batch of frames). */
  siblings: Artifact[]
}

const RENDERERS: Record<string, (props: RendererProps) => ReactNode> = {
  image: ImageRenderer,
  mask: ImageRenderer,
  video: VideoRenderer,
  audio: AudioRenderer,
  string: TextRenderer,
  int: TextRenderer,
  float: TextRenderer,
  boolean: TextRenderer,
  latent: OpaqueRenderer,
  file: OpaqueRenderer,
}

export function ArtifactPreview({ projectId, artifacts }: { projectId: string; artifacts: Artifact[] }) {
  if (!artifacts.length) {
    return <Empty title="No output yet">Run this step to see its results here.</Empty>
  }

  // Group by port so a batch of frames shows as one sequence rather than N separate cards.
  const byPort = new Map<string, Artifact[]>()
  for (const artifact of artifacts) {
    byPort.set(artifact.port_key, [...(byPort.get(artifact.port_key) ?? []), artifact])
  }

  return (
    <div className="space-y-3 p-3">
      {[...byPort.entries()].map(([port, group]) => {
        const first = group[0]
        const Renderer = RENDERERS[first.kind] ?? OpaqueRenderer
        return (
          <div key={port}>
            <div className="mb-1.5 flex items-center gap-2">
              <span
                className="grid size-4 place-items-center rounded text-[10px]"
                style={{ background: `${KIND_COLOR[first.kind]}22`, color: KIND_COLOR[first.kind] }}
              >
                {KIND_ICON[first.kind] ?? '•'}
              </span>
              <span className="text-xs font-medium">{port}</span>
              <Badge tone="muted">{first.kind}</Badge>
              {group.length > 1 && <Badge tone="info">{group.length} frames</Badge>}
            </div>
            <Renderer projectId={projectId} artifact={first} siblings={group} />
          </div>
        )
      })}
    </div>
  )
}

function ImageRenderer({ projectId, artifact, siblings }: RendererProps) {
  const [index, setIndex] = useState(0)
  const current = siblings[Math.min(index, siblings.length - 1)]
  const url = api.media.url(projectId, current.path)

  return (
    <div className="overflow-hidden rounded-md border border-[var(--color-edge)] bg-black/40">
      <a href={url} target="_blank" rel="noreferrer">
        <img src={url} alt={artifact.port_key} className="max-h-72 w-full object-contain" />
      </a>
      {siblings.length > 1 && (
        <div className="flex items-center gap-2 border-t border-[var(--color-edge)] px-2 py-1.5">
          <input
            type="range"
            className="flex-1"
            min={0}
            max={siblings.length - 1}
            value={index}
            onChange={(e) => setIndex(Number(e.target.value))}
          />
          <span className="w-16 text-right text-[10px] text-[var(--color-ink-dim)]">
            {index + 1} / {siblings.length}
          </span>
        </div>
      )}
      <Meta artifact={current} />
    </div>
  )
}

function VideoRenderer({ projectId, artifact }: RendererProps) {
  return (
    <div className="overflow-hidden rounded-md border border-[var(--color-edge)] bg-black/40">
      <video
        src={api.media.url(projectId, artifact.path)}
        controls
        loop
        className="max-h-72 w-full"
      />
      <Meta artifact={artifact} />
    </div>
  )
}

function AudioRenderer({ projectId, artifact }: RendererProps) {
  return (
    <div className="rounded-md border border-[var(--color-edge)] bg-black/20 p-2">
      <audio src={api.media.url(projectId, artifact.path)} controls className="w-full" />
      <Meta artifact={artifact} />
    </div>
  )
}

function TextRenderer({ projectId, artifact }: RendererProps) {
  const inline = artifact.meta?.value
  return (
    <div className="rounded-md border border-[var(--color-edge)] bg-[var(--color-surface)] p-2">
      {inline !== undefined ? (
        <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-[var(--color-ink)]">
          {String(inline)}
        </pre>
      ) : (
        <a
          className="text-xs text-[var(--color-accent)] hover:underline"
          href={api.media.url(projectId, artifact.path)}
          target="_blank"
          rel="noreferrer"
        >
          Open text file
        </a>
      )}
    </div>
  )
}

function OpaqueRenderer({ projectId, artifact }: RendererProps) {
  return (
    <a
      href={api.media.url(projectId, artifact.path)}
      download
      className={cx(
        'flex items-center gap-2 rounded-md border border-[var(--color-edge)] bg-[var(--color-surface)]',
        'px-3 py-2 text-xs hover:border-[var(--color-accent)]/60',
      )}
    >
      <span>{KIND_ICON[artifact.kind] ?? '📄'}</span>
      <span className="flex-1 truncate font-mono text-[11px]">{artifact.path.split('/').pop()}</span>
      {artifact.meta?.size ? (
        <span className="text-[var(--color-ink-dim)]">{formatBytes(artifact.meta.size)}</span>
      ) : null}
    </a>
  )
}

function Meta({ artifact }: { artifact: Artifact }) {
  const meta = artifact.meta ?? {}
  const bits = [
    meta.width && meta.height ? `${meta.width}×${meta.height}` : null,
    meta.fps ? `${Number(meta.fps).toFixed(2)} fps` : null,
    meta.duration ? `${Number(meta.duration).toFixed(2)}s` : null,
    meta.sample_rate ? `${meta.sample_rate} Hz` : null,
    meta.size ? formatBytes(meta.size) : null,
  ].filter(Boolean)

  if (!bits.length) return null
  return (
    <div className="border-t border-[var(--color-edge)] px-2 py-1 text-[10px] text-[var(--color-ink-dim)]">
      {bits.join(' · ')}
    </div>
  )
}
