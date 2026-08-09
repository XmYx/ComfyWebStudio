import { useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { Clip, Project, RenderRequest, ResolvedTimeline, Track, TrackKind } from '@/api/types'
import { KIND_COLOR } from '@/lib/kinds'
import { formatBytes, formatTimecode } from '@/lib/format'
import { useStudio } from '@/store/studio'
import { useLayout } from '@/store/layout'
import { RenderDialog } from './RenderDialog'
import {
  Button, Callout, Empty, Field, Modal, Panel, PanelHeader, ProgressBar,
  Select, TextInput, cx, useToast,
} from '@/components/ui'
import { ContextMenu, useContextMenu, type MenuItem } from '@/components/ContextMenu'
import { useCommandContext } from '@/features/menu/useCommandContext'

const MIN_PX_PER_SECOND = 8
const MAX_PX_PER_SECOND = 400
const TRACK_HEIGHT = 56
const HEADER_WIDTH = 140

export function TimelinePage() {
  const { projectId } = useParams<{ projectId: string }>()
  const toast = useToast()
  const queryClient = useQueryClient()

  const [zoom, setZoom] = useState(40)
  const [playhead, setPlayhead] = useState(0)
  const [adding, setAdding] = useState(false)
  const renderProgress = useStudio((s) => s.renderProgress)
  const selectedClip = useStudio((s) => s.selectedClip)
  const selectClip = useStudio((s) => s.selectClip)
  // Kept in the shared dialog slot so a menu command can open it from anywhere.
  const dialog = useLayout((s) => s.dialog)
  const openDialog = useLayout((s) => s.openDialog)
  const closeDialog = useLayout((s) => s.closeDialog)
  const commandContext = useCommandContext()
  const contextMenu = useContextMenu()

  const { data: project } = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => api.projects.get(projectId!),
    enabled: Boolean(projectId),
  })

  const { data: resolved } = useQuery({
    queryKey: ['timeline-resolved', projectId, project?.modified],
    queryFn: () => api.timeline.resolved(projectId!),
    enabled: Boolean(projectId),
  })

  const { data: renders } = useQuery({
    queryKey: ['renders', projectId],
    queryFn: () => api.timeline.renders(projectId!),
    enabled: Boolean(projectId),
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['project', projectId] })
    queryClient.invalidateQueries({ queryKey: ['timeline-resolved', projectId] })
  }

  const buildFromShots = useMutation({
    mutationFn: () => api.timeline.fromShots(projectId!),
    onSuccess: () => { toast.push('ok', 'Timeline built from shots.'); invalidate() },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  const addTrack = useMutation({
    mutationFn: (kind: TrackKind) => api.timeline.createTrack(projectId!, kind),
    onSuccess: invalidate,
  })

  const render = useMutation({
    mutationFn: (body: RenderRequest) => api.timeline.render(projectId!, body),
    onSuccess: (result) => {
      if (result.outputs > 1) toast.push('info', `Rendering ${result.outputs} files…`)
    },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  // The clip the dialog offers as "selected clip", resolved from the timeline selection.
  const selection = useMemo(() => {
    if (!project || !selectedClip) return null
    const track = project.timeline.tracks.find((t) => t.id === selectedClip.trackId)
    const clip = track?.clips.find((c) => c.id === selectedClip.clipId)
    return track && clip ? { track: track.id, clip } : null
  }, [project, selectedClip])

  if (!project) return <Empty title="Loading…" />

  const timeline = project.timeline
  const duration = Math.max(timeline.duration, 10)
  const errors = (resolved?.clips ?? []).filter((c) => c.error)

  return (
    <div className="grid h-full grid-rows-[auto_1fr_auto] gap-2 p-2">
      {/* Toolbar */}
      <Panel className="flex items-center gap-2 px-3 py-2">
        <Button size="sm" onClick={() => buildFromShots.mutate()}>Build from shots</Button>
        <Button size="sm" variant="ghost" onClick={() => setAdding(true)}>+ Clip</Button>
        <Select
          className="w-32 shrink-0"
          value=""
          onChange={(e) => e.target.value && addTrack.mutate(e.target.value as TrackKind)}
        >
          <option value="">+ Track…</option>
          <option value="video">Video</option>
          <option value="audio">Audio</option>
          <option value="text">Text</option>
          <option value="overlay">Overlay</option>
        </Select>

        <div className="mx-2 h-5 w-px bg-[var(--color-edge)]" />

        <span className="text-xs text-[var(--color-ink-dim)]">
          {timeline.width}×{timeline.height} · {timeline.fps} fps · {formatTimecode(timeline.duration, timeline.fps)}
        </span>

        <div className="flex-1" />

        <label className="flex items-center gap-2 text-xs text-[var(--color-ink-dim)]">
          Zoom
          <input
            type="range"
            min={MIN_PX_PER_SECOND}
            max={MAX_PX_PER_SECOND}
            value={zoom}
            onChange={(e) => setZoom(Number(e.target.value))}
            className="w-28"
          />
        </label>

        <Button
          size="sm"
          variant="ghost"
          onClick={() => render.mutate({ still: true, time_s: playhead })}
          title="Export the frame at the playhead as a PNG"
        >
          Still
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={timeline.duration <= 0 || Boolean(renderProgress)}
          onClick={() => render.mutate({ scope: 'timeline' })}
          title="Render the whole timeline with the project's settings"
        >
          Render
        </Button>
        <Button
          size="sm"
          variant="primary"
          disabled={timeline.duration <= 0 || Boolean(renderProgress)}
          onClick={() => openDialog('render')}
          title="Choose what to render and how"
        >
          Render…
        </Button>
      </Panel>

      {renderProgress && (
        <div className="px-1">
          <ProgressBar value={renderProgress.progress} />
        </div>
      )}

      {/* Tracks */}
      <Panel className="flex min-h-0 flex-col overflow-hidden">
        {errors.length > 0 && (
          <div className="border-b border-[var(--color-edge)] p-2">
            <Callout tone="warn" title={`${errors.length} clip(s) cannot be rendered`}>
              {errors[0].error}
            </Callout>
          </div>
        )}

        {!timeline.tracks.length ? (
          <Empty title="The timeline is empty">
            Use “Build from shots” to lay every shot's final output end to end, or add a track and place
            clips yourself.
          </Empty>
        ) : (
          <TrackArea
            project={project}
            resolved={resolved}
            zoom={zoom}
            duration={duration}
            playhead={playhead}
            onScrub={setPlayhead}
            onChanged={invalidate}
            selectedClip={selectedClip}
            onSelectClip={selectClip}
            onContextMenu={contextMenu.open}
          />
        )}
      </Panel>

      {/* Bottom: inspector + renders */}
      <div className="grid grid-cols-[1fr_320px] gap-2">
        <ClipInspector project={project} onChanged={invalidate} />
        <Panel className="max-h-52 overflow-hidden">
          <PanelHeader>Renders</PanelHeader>
          <div className="max-h-40 overflow-y-auto p-2">
            {!renders?.length ? (
              <div className="text-xs text-[var(--color-ink-dim)]">Nothing rendered yet.</div>
            ) : (
              renders.map((item) => (
                <a
                  key={item.path}
                  href={api.media.url(project.id, item.path)}
                  target="_blank"
                  rel="noreferrer"
                  className="flex items-center justify-between rounded px-2 py-1 text-xs hover:bg-[var(--color-panel-2)]"
                >
                  <span className="truncate">{item.name}</span>
                  <span className="text-[10px] text-[var(--color-ink-dim)]">{formatBytes(item.size)}</span>
                </a>
              ))
            )}
          </div>
        </Panel>
      </div>

      <AddClipModal
        open={adding}
        onClose={() => setAdding(false)}
        project={project}
        onChanged={invalidate}
      />

      <RenderDialog
        open={dialog === 'render'}
        onClose={closeDialog}
        project={project}
        playhead={playhead}
        selectedClip={selection}
        onRender={(body) => render.mutate(body)}
      />
      <ContextMenu state={contextMenu.menu} onClose={contextMenu.close} context={commandContext} />
    </div>
  )
}

// -- track area -----------------------------------------------------------------------------------------

function TrackArea({
  project, resolved, zoom, duration, playhead, onScrub, onChanged, selectedClip, onSelectClip,
  onContextMenu,
}: {
  project: Project
  resolved: ResolvedTimeline | undefined
  zoom: number
  duration: number
  playhead: number
  onScrub: (t: number) => void
  onChanged: () => void
  selectedClip: { trackId: string; clipId: string } | null
  onSelectClip: (selection: { trackId: string; clipId: string } | null) => void
  onContextMenu: (event: React.MouseEvent, items: MenuItem[]) => void
}) {
  const clipMenu = (track: Track, clip: Clip): MenuItem[] => [
    { type: 'header', label: clip.name || 'Clip' },
    { type: 'command', id: 'edit.copy' },
    { type: 'command', id: 'edit.cut' },
    { type: 'command', id: 'edit.paste' },
    { type: 'separator' },
    // The command reads the current selection, and right-clicking a clip selects it.
    { type: 'command', id: 'render.clip' },
    { type: 'command', id: 'render.dialog' },
    { type: 'separator' },
    {
      type: 'action',
      label: clip.enabled ? 'Disable clip' : 'Enable clip',
      checked: clip.enabled,
      onSelect: async () => {
        await api.timeline.updateClip(project.id, track.id, clip.id, { enabled: !clip.enabled })
        onChanged()
      },
    },
    {
      type: 'action',
      label: 'Duplicate clip',
      onSelect: async () => {
        await api.timeline.createClip(project.id, track.id, {
          source: clip.source, duration: clip.duration, name: clip.name, text: clip.text,
          start: clip.start + clip.duration,
        })
        onChanged()
      },
    },
    {
      type: 'action',
      label: 'Snap to the previous clip',
      onSelect: async () => {
        const before = track.clips.filter((c) => c.id !== clip.id && c.start < clip.start)
        const end = before.length ? Math.max(...before.map((c) => c.start + c.duration)) : 0
        await api.timeline.updateClip(project.id, track.id, clip.id, { start: end })
        onChanged()
      },
    },
    { type: 'separator' },
    {
      type: 'action',
      label: 'Delete clip',
      danger: true,
      onSelect: async () => {
        await api.timeline.removeClip(project.id, track.id, clip.id)
        onSelectClip(null)
        onChanged()
      },
    },
  ]

  const trackMenu = (track: Track): MenuItem[] => [
    { type: 'header', label: track.name },
    {
      type: 'action',
      label: 'Rename…',
      onSelect: async () => {
        const name = prompt('Track name', track.name)
        if (name && name !== track.name) {
          await api.timeline.updateTrack(project.id, track.id, { name })
          onChanged()
        }
      },
    },
    {
      type: 'action',
      label: track.muted ? 'Unmute' : 'Mute',
      checked: !track.muted,
      onSelect: async () => {
        await api.timeline.updateTrack(project.id, track.id, { muted: !track.muted })
        onChanged()
      },
    },
    {
      type: 'action',
      label: track.locked ? 'Unlock' : 'Lock',
      checked: track.locked,
      onSelect: async () => {
        await api.timeline.updateTrack(project.id, track.id, { locked: !track.locked })
        onChanged()
      },
    },
    { type: 'command', id: 'edit.paste' },
    { type: 'separator' },
    {
      type: 'action',
      label: 'Delete track',
      danger: true,
      onSelect: async () => {
        if (!confirm(`Delete track “${track.name}” and its clips?`)) return
        await api.timeline.removeTrack(project.id, track.id)
        onChanged()
      },
    },
  ]

  const laneMenu = (track: Track): MenuItem[] => [
    { type: 'header', label: track.name },
    { type: 'command', id: 'edit.paste' },
    ...trackMenu(track).slice(1),
  ]
  const scrollRef = useRef<HTMLDivElement>(null)
  const width = duration * zoom

  const clipStatus = useMemo(() => {
    const map = new Map<string, { error: string | null; kind: string | null; thumb: string | null }>()
    for (const entry of resolved?.clips ?? []) {
      map.set(entry.clip_id, {
        error: entry.error,
        kind: entry.kind,
        thumb: entry.artifacts[0]?.thumb ?? null,
      })
    }
    return map
  }, [resolved])

  const scrub = (event: React.MouseEvent) => {
    const rect = event.currentTarget.getBoundingClientRect()
    onScrub(Math.max(0, (event.clientX - rect.left + (scrollRef.current?.scrollLeft ?? 0)) / zoom))
  }

  return (
    <div className="flex min-h-0 flex-1">
      {/* Track headers stay pinned while the lanes scroll. */}
      <div className="shrink-0 border-r border-[var(--color-edge)]" style={{ width: HEADER_WIDTH }}>
        <div className="h-7 border-b border-[var(--color-edge)]" />
        {project.timeline.tracks.map((track) => (
          <div key={track.id} onContextMenu={(event) => onContextMenu(event, trackMenu(track))}>
            <TrackHeader project={project} track={track} onChanged={onChanged} />
          </div>
        ))}
      </div>

      <div ref={scrollRef} className="min-w-0 flex-1 overflow-auto">
        <div style={{ width: Math.max(width, 600) }}>
          <Ruler duration={duration} zoom={zoom} fps={project.timeline.fps} onScrub={scrub} />

          <div className="relative">
            {project.timeline.tracks.map((track) => (
              <div
                key={track.id}
                className="relative border-b border-[var(--color-edge)]"
                style={{ height: TRACK_HEIGHT }}
                onContextMenu={(event) => onContextMenu(event, laneMenu(track))}
              >
                {track.clips.map((clip) => (
                  <ClipBlock
                    key={clip.id}
                    project={project}
                    track={track}
                    clip={clip}
                    zoom={zoom}
                    status={clipStatus.get(clip.id)}
                    selected={selectedClip?.clipId === clip.id}
                    onSelect={() => onSelectClip({ trackId: track.id, clipId: clip.id })}
                    onChanged={onChanged}
                    onContextMenu={(event) => {
                      onSelectClip({ trackId: track.id, clipId: clip.id })
                      onContextMenu(event, clipMenu(track, clip))
                    }}
                  />
                ))}
              </div>
            ))}

            <div
              className="pointer-events-none absolute inset-y-0 w-px bg-[var(--color-accent)]"
              style={{ left: playhead * zoom }}
            />
          </div>
        </div>
      </div>
    </div>
  )
}

function Ruler({
  duration, zoom, fps, onScrub,
}: { duration: number; zoom: number; fps: number; onScrub: (e: React.MouseEvent) => void }) {
  // Choose a tick interval that keeps labels ~60px apart at any zoom.
  const candidates = [0.5, 1, 2, 5, 10, 30, 60, 120]
  const interval = candidates.find((c) => c * zoom >= 60) ?? 300
  const ticks = Math.ceil(duration / interval)

  return (
    <div
      className="relative h-7 cursor-pointer border-b border-[var(--color-edge)] bg-[var(--color-panel-2)]"
      onClick={onScrub}
    >
      {Array.from({ length: ticks + 1 }, (_, i) => (
        <div key={i} className="absolute top-0 h-full" style={{ left: i * interval * zoom }}>
          <div className="h-2 w-px bg-[var(--color-edge)]" />
          <span className="ml-1 text-[9px] text-[var(--color-ink-dim)]">
            {formatTimecode(i * interval, fps)}
          </span>
        </div>
      ))}
    </div>
  )
}

function TrackHeader({
  project, track, onChanged,
}: { project: Project; track: Track; onChanged: () => void }) {
  return (
    <div
      className="flex items-center gap-1 border-b border-[var(--color-edge)] px-2"
      style={{ height: TRACK_HEIGHT }}
    >
      <div className="min-w-0 flex-1">
        <div className="truncate text-xs">{track.name}</div>
        <div className="text-[10px] text-[var(--color-ink-dim)]">{track.kind}</div>
      </div>
      <button
        title={track.muted ? 'Unmute' : 'Mute'}
        className={cx('px-1 text-xs', track.muted ? 'text-[var(--color-bad)]' : 'text-[var(--color-ink-dim)]')}
        onClick={async () => {
          await api.timeline.updateTrack(project.id, track.id, { muted: !track.muted })
          onChanged()
        }}
      >
        {track.muted ? '🔇' : '🔊'}
      </button>
      <button
        title="Delete track"
        className="px-1 text-xs text-[var(--color-ink-dim)] hover:text-[var(--color-bad)]"
        onClick={async () => {
          if (!confirm(`Delete track “${track.name}” and its clips?`)) return
          await api.timeline.removeTrack(project.id, track.id)
          onChanged()
        }}
      >
        ✕
      </button>
    </div>
  )
}

function ClipBlock({
  project, track, clip, zoom, status, selected, onSelect, onChanged, onContextMenu,
}: {
  project: Project
  track: Track
  clip: Clip
  zoom: number
  status: { error: string | null; kind: string | null; thumb: string | null } | undefined
  selected: boolean
  onSelect: () => void
  onChanged: () => void
  onContextMenu: (event: React.MouseEvent) => void
}) {
  const dragState = useRef<{ mode: 'move' | 'trim'; x: number; start: number; duration: number } | null>(null)

  const beginDrag = (event: React.MouseEvent, mode: 'move' | 'trim') => {
    event.stopPropagation()
    if (track.locked) return
    dragState.current = { mode, x: event.clientX, start: clip.start, duration: clip.duration }

    const onMove = (move: MouseEvent) => {
      const state = dragState.current
      if (!state) return
      const delta = (move.clientX - state.x) / zoom
      const element = document.getElementById(`clip-${clip.id}`)
      if (!element) return
      if (state.mode === 'move') {
        element.style.left = `${Math.max(0, state.start + delta) * zoom}px`
      } else {
        element.style.width = `${Math.max(0.04, state.duration + delta) * zoom}px`
      }
    }

    const onUp = async (up: MouseEvent) => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      const state = dragState.current
      dragState.current = null
      if (!state) return

      const delta = (up.clientX - state.x) / zoom
      if (Math.abs(delta) < 0.01) return
      // Persist once, on release — dragging fires far too often to PATCH every frame.
      const patch =
        state.mode === 'move'
          ? { start: Math.max(0, state.start + delta) }
          : { duration: Math.max(0.04, state.duration + delta) }
      await api.timeline.updateClip(project.id, track.id, clip.id, patch)
      onChanged()
    }

    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

  const color = status?.kind ? KIND_COLOR[status.kind] ?? 'var(--kind-file)' : 'var(--kind-file)'

  return (
    <div
      id={`clip-${clip.id}`}
      onMouseDown={(e) => e.button === 0 && beginDrag(e, 'move')}
      onClick={onSelect}
      onContextMenu={onContextMenu}
      className={cx(
        'absolute top-1 flex h-[calc(100%-8px)] cursor-grab items-center overflow-hidden rounded border',
        selected ? 'border-[var(--color-accent)] ring-1 ring-[var(--color-accent)]' : 'border-[var(--color-edge)]',
        status?.error && 'border-[var(--color-bad)]',
        !clip.enabled && 'opacity-40',
      )}
      style={{
        left: clip.start * zoom,
        width: Math.max(12, clip.duration * zoom),
        background: `linear-gradient(180deg, ${color}33, ${color}18)`,
      }}
      title={status?.error ?? `${clip.name || clip.id} · ${clip.duration.toFixed(2)}s`}
    >
      {status?.thumb && (
        <img
          src={api.media.url(project.id, status.thumb)}
          alt=""
          className="h-full w-10 shrink-0 object-cover opacity-80"
        />
      )}
      <span className="truncate px-1.5 text-[10px]">
        {status?.error ? '⚠ ' : ''}{clip.name || clip.text || clip.source.port_key || 'clip'}
      </span>
      <div
        onMouseDown={(e) => beginDrag(e, 'trim')}
        className="absolute right-0 top-0 h-full w-1.5 cursor-ew-resize bg-white/20 hover:bg-white/40"
      />
    </div>
  )
}

// -- clip inspector -------------------------------------------------------------------------------------

function ClipInspector({ project, onChanged }: { project: Project; onChanged: () => void }) {
  const selection = useStudio((s) => s.selectedClip)
  const selectClip = useStudio((s) => s.selectClip)

  const track = project.timeline.tracks.find((t) => t.id === selection?.trackId)
  const clip = track?.clips.find((c) => c.id === selection?.clipId)

  if (!clip || !track) {
    return (
      <Panel className="max-h-52">
        <PanelHeader>Clip</PanelHeader>
        <div className="p-3 text-xs text-[var(--color-ink-dim)]">
          Select a clip to adjust its timing, opacity or text.
        </div>
      </Panel>
    )
  }

  const patch = async (body: Partial<Clip>) => {
    await api.timeline.updateClip(project.id, track.id, clip.id, body)
    onChanged()
  }

  return (
    <Panel className="max-h-52 overflow-y-auto">
      <PanelHeader
        actions={
          <Button
            size="sm"
            variant="danger"
            onClick={async () => {
              await api.timeline.removeClip(project.id, track.id, clip.id)
              selectClip(null)
              onChanged()
            }}
          >
            Delete
          </Button>
        }
      >
        {clip.name || 'Clip'}
      </PanelHeader>

      <div className="grid grid-cols-4 gap-3 p-3">
        <Field label="Start (s)">
          <TextInput
            type="number" step={0.1} value={clip.start}
            onChange={(e) => patch({ start: Number(e.target.value) })}
          />
        </Field>
        <Field label="Duration (s)">
          <TextInput
            type="number" step={0.1} min={0.04} value={clip.duration}
            onChange={(e) => patch({ duration: Math.max(0.04, Number(e.target.value)) })}
          />
        </Field>
        <Field label="Opacity">
          <TextInput
            type="number" step={0.05} min={0} max={1} value={clip.opacity}
            onChange={(e) => patch({ opacity: Number(e.target.value) })}
          />
        </Field>
        <Field label="Fit">
          <Select
            value={clip.transform.fit}
            onChange={(e) => patch({ transform: { ...clip.transform, fit: e.target.value } })}
          >
            <option value="contain">Contain</option>
            <option value="cover">Cover</option>
            <option value="stretch">Stretch</option>
            <option value="none">None</option>
          </Select>
        </Field>

        {track.kind === 'text' && (
          <div className="col-span-4">
            <Field label="Text">
              <TextInput value={clip.text} onChange={(e) => patch({ text: e.target.value })} />
            </Field>
          </div>
        )}

        {track.kind === 'audio' && (
          <Field label="Volume">
            <TextInput
              type="number" step={0.05} min={0} max={2} value={clip.volume}
              onChange={(e) => patch({ volume: Number(e.target.value) })}
            />
          </Field>
        )}

        <Field label="Fade in (s)">
          <TextInput
            type="number" step={0.1} min={0} value={clip.transition_in.duration}
            onChange={(e) =>
              patch({ transition_in: { kind: Number(e.target.value) > 0 ? 'fade' : 'none', duration: Number(e.target.value) } })
            }
          />
        </Field>
        <Field label="Fade out (s)">
          <TextInput
            type="number" step={0.1} min={0} value={clip.transition_out.duration}
            onChange={(e) =>
              patch({ transition_out: { kind: Number(e.target.value) > 0 ? 'fade' : 'none', duration: Number(e.target.value) } })
            }
          />
        </Field>
      </div>
    </Panel>
  )
}

// -- add clip -------------------------------------------------------------------------------------------

function AddClipModal({
  open, onClose, project, onChanged,
}: { open: boolean; onClose: () => void; project: Project; onChanged: () => void }) {
  const toast = useToast()
  const [trackId, setTrackId] = useState('')
  const [stepKey, setStepKey] = useState('')

  // Every (shot, step, port) that could feed a clip.
  const sources = useMemo(() => {
    const out: Array<{ key: string; label: string; shot: string; step: string; port: string; kind: string }> = []
    for (const shot of project.shots) {
      for (const step of shot.steps) {
        const workflow = project.workflows[step.workflow_id]
        for (const port of workflow?.ports.filter((p) => p.direction === 'out') ?? []) {
          out.push({
            key: `${shot.id}|${step.id}|${port.key}`,
            label: `${shot.name} · ${step.name} · ${port.label || port.key}`,
            shot: shot.id, step: step.id, port: port.key, kind: port.kind,
          })
        }
      }
    }
    return out
  }, [project])

  const add = async () => {
    const source = sources.find((s) => s.key === stepKey)
    const track = project.timeline.tracks.find((t) => t.id === trackId)
    if (!track) return toast.push('bad', 'Choose a track.')

    try {
      await api.timeline.createClip(project.id, track.id, {
        source: source
          ? { kind: 'step_output', shot_id: source.shot, step_id: source.step, port_key: source.port, asset_id: null }
          : undefined,
        name: source?.label.split(' · ').slice(-2).join(' · ') ?? 'Text',
      })
      onChanged()
      onClose()
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="Add a clip">
      <div className="space-y-3">
        <Field label="Track">
          <Select value={trackId} onChange={(e) => setTrackId(e.target.value)}>
            <option value="">Choose…</option>
            {project.timeline.tracks.map((track) => (
              <option key={track.id} value={track.id}>{track.name} ({track.kind})</option>
            ))}
          </Select>
        </Field>

        <Field label="Source" hint="a step's output port">
          <Select value={stepKey} onChange={(e) => setStepKey(e.target.value)}>
            <option value="">None (text clip)</option>
            {sources.map((source) => (
              <option key={source.key} value={source.key}>
                {source.label} ({source.kind})
              </option>
            ))}
          </Select>
        </Field>

        <div className="text-xs text-[var(--color-ink-dim)]">
          A clip follows its step: re-running the shot updates what the clip shows, without touching the edit.
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!trackId} onClick={add}>Add</Button>
        </div>
      </div>
    </Modal>
  )
}
