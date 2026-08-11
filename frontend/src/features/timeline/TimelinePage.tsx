import { useEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type {
  Clip, PortKind, Project, RenderRequest, ResolvedTimeline, Track, TrackKind,
} from '@/api/types'
import { KIND_COLOR } from '@/lib/kinds'
import { formatBytes, formatTimecode } from '@/lib/format'
import { useStudio } from '@/store/studio'
import { useLayout } from '@/store/layout'
import { RenderDialog } from './RenderDialog'
import { Monitor } from './Monitor'
import {
  Button, Callout, Empty, Field, Modal, Panel, PanelHeader, ProgressBar,
  Select, TextInput, cx, useToast,
} from '@/components/ui'
import { ContextMenu, useContextMenu, type MenuItem } from '@/components/ContextMenu'
import { isOurDrag, readDrag } from '@/lib/dnd'
import { snap, snapMove, snapTargets } from './snapping'
import { TrackMixer } from './TrackMixer'
import { Waveform } from './Waveform'
import { TimelineAudio } from './TimelineAudio'
import { useCommandContext } from '@/features/menu/useCommandContext'

const MIN_PX_PER_SECOND = 8
const MAX_PX_PER_SECOND = 400
/**
 * What each kind of track can actually show.
 *
 * Not the same question as whether two ports may be chained: a still image on a video track is normal —
 * it is simply held for the clip's duration — whereas a string output there has nothing to display.
 */
const TRACK_ACCEPTS: Partial<Record<TrackKind, PortKind[]>> = {
  video: ['image', 'video'],
  overlay: ['image', 'video'],
  audio: ['audio'],
  text: ['string', 'int', 'float', 'boolean'],
}

const TRACK_HEIGHT = 56
const HEADER_WIDTH = 210

/**
 * The timeline.
 *
 * `embedded` drops the surrounding panels — monitor, clip inspector, renders — because in the docked
 * workspace each of those is a widget of its own and duplicating them here would be noise.
 */
export function TimelinePage({ embedded = false }: { embedded?: boolean } = {}) {
  const { projectId } = useParams<{ projectId: string }>()
  const toast = useToast()
  const queryClient = useQueryClient()

  const [zoom, setZoom] = useState(40)
  const [adding, setAdding] = useState(false)
  // Snapping is on by default and held down with Alt, which is the convention everywhere else.
  const [snapping, setSnapping] = useState(true)
  // The transport lives in the shared store so a floating Monitor widget follows the same playhead.
  const playhead = useStudio((s) => s.playhead)
  const setPlayhead = useStudio((s) => s.setPlayhead)
  const playing = useStudio((s) => s.playing)
  const setPlaying = useStudio((s) => s.setPlaying)
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
    <div
      className={cx('grid h-full gap-2', embedded ? 'p-0' : 'p-2')}
      style={{ gridTemplateRows: embedded ? 'auto 1fr' : 'auto 1fr auto' }}
    >
      {/* Toolbar */}
      <Panel className="flex items-center gap-2 px-3 py-2">
        <Button size="sm" onClick={() => buildFromShots.mutate()}>Build from shots</Button>
        <Button size="sm" variant="ghost" onClick={() => setAdding(true)}>+ Clip</Button>
        <Button
          size="sm"
          variant={snapping ? 'default' : 'ghost'}
          title="Snap edges to other clips and the playhead — hold Alt to override"
          onClick={() => setSnapping((on) => !on)}
        >
          ⇥ Snap
        </Button>
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
            onScrub={(time) => { setPlaying(false); setPlayhead(time) }}
            onChanged={invalidate}
            selectedClip={selectedClip}
            onSelectClip={selectClip}
            onContextMenu={contextMenu.open}
            snapping={snapping}
          />
        )}
      </Panel>

      {/* Bottom: monitor, inspector and renders. Each is its own widget in the docked workspace. */}
      {!embedded && (
      <div className="grid grid-cols-[320px_1fr_320px] gap-2">
        <Panel className="flex max-h-52 min-h-0 flex-col overflow-hidden">
          <PanelHeader>Monitor</PanelHeader>
          <Monitor
            className="min-h-0 flex-1"
            project={project}
            resolved={resolved}
            playhead={playhead}
            onScrub={setPlayhead}
            playing={playing}
            onPlayingChange={setPlaying}
          />
        </Panel>
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
      )}

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
      {/* Renders nothing; it is what makes the timeline audible as it plays. */}
      <TimelineAudio project={project} resolved={resolved} />

      <ContextMenu state={contextMenu.menu} onClose={contextMenu.close} context={commandContext} />
    </div>
  )
}

/** Finished renders, as a panel of its own. */
export function RendersPanel({ project }: { project: Project }) {
  const { data: renders } = useQuery({
    queryKey: ['renders', project.id],
    queryFn: () => api.timeline.renders(project.id),
  })

  return (
    <Panel className="flex h-full min-h-0 flex-col overflow-hidden">
      <PanelHeader>Renders</PanelHeader>
      <div className="min-h-0 flex-1 overflow-y-auto p-2">
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
  )
}

/** What the lanes need to know about each clip's resolved media. */
interface ClipStatus {
  error: string | null
  kind: string | null
  thumb: string | null
  audioPath: string | null
  /** The media's own metadata, which is where a clip's natural length comes from. */
  meta: Record<string, unknown>
  /** How many artifacts the port produced — a sequence's length is its frame count. */
  frames: number
}

// -- track area -----------------------------------------------------------------------------------------

function TrackArea({
  project, resolved, zoom, duration, playhead, onScrub, onChanged, selectedClip, onSelectClip,
  onContextMenu, snapping,
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
  snapping: boolean
}) {
  const toast = useToast()
  //: Where the drag in progress has snapped to, drawn as a guide so the alignment is visible.
  const [snapGuide, setSnapGuide] = useState<number | null>(null)
  //: A span of *time* selected by dragging across empty space, which ripple delete then removes.
  const [range, setRange] = useState<{ from: number; to: number } | null>(null)

  /**
   * Dragging across empty lane space selects a span of time.
   *
   * Distinct from selecting a clip: what is selected here is the gap itself, which is the thing a ripple
   * delete acts on. It snaps to the same targets as a clip drag, so a span can be taken out exactly
   * between two cuts.
   */
  const beginRange = (event: React.MouseEvent) => {
    if (event.button !== 0) return
    const lane = event.currentTarget as HTMLElement
    const box = lane.getBoundingClientRect()
    const targets = snapTargets(project.timeline, playhead)
    const at = (clientX: number) =>
      snap(Math.max(0, (clientX - box.left) / zoom), targets, zoom, snapping).time

    const anchor = at(event.clientX)
    setRange({ from: anchor, to: anchor })

    const move = (e: MouseEvent) => {
      const now = at(e.clientX)
      setRange({ from: Math.min(anchor, now), to: Math.max(anchor, now) })
    }
    const up = (e: MouseEvent) => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
      const now = at(e.clientX)
      // A click, not a drag: clear rather than leaving a zero-width selection nobody can see.
      if (Math.abs(now - anchor) * zoom < 3) setRange(null)
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  useEffect(() => {
    if (!range || range.to <= range.from) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Delete' && event.key !== 'Backspace') return
      // Not while typing in the inspector: Backspace there means backspace.
      const target = event.target as HTMLElement | null
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return
      event.preventDefault()
      void rippleDelete()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  const rippleDelete = async (trackId?: string) => {
    if (!range || range.to <= range.from) return
    try {
      await api.timeline.rippleDelete(project.id, {
        start: range.from, end: range.to, track_id: trackId,
      })
      setRange(null)
      onChanged()
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    }
  }
  //: The lane a drag is currently over, so it can be highlighted as a target.
  const [dropTrack, setDropTrack] = useState<string | null>(null)

  /**
   * What this clip could sensibly be tied to: clips on *other* tracks that overlap it in time.
   *
   * Overlap is the honest test — two clips that never share a moment have no reason to move together,
   * and offering every clip in the project would make the menu useless.
   */
  const tieCandidates = (clip: Clip) =>
    project.timeline.tracks.flatMap((other) =>
      other.clips
        .filter(
          (candidate) =>
            candidate.id !== clip.id &&
            !candidate.link_id &&
            !other.clips.includes(clip) &&
            candidate.start < clip.start + clip.duration &&
            candidate.start + candidate.duration > clip.start,
        )
        .map((candidate) => ({ track: other, clip: candidate })),
    )

  /** The media's own length, when it has one — an image has none, so it keeps whatever it was given. */
  const sourceLength = (clip: Clip): number | null => {
    const status = clipStatus.get(clip.id)
    const meta = status?.meta ?? {}
    if (typeof meta.duration === 'number' && meta.duration > 0) return meta.duration
    if ((status?.frames ?? 0) > 1) return (status!.frames as number) / Math.max(1, project.timeline.fps)
    return null
  }

  const clipMenu = (track: Track, clip: Clip): MenuItem[] => [
    { type: 'header', label: clip.name || 'Clip' },
    { type: 'command', id: 'edit.copy' },
    { type: 'command', id: 'edit.cut' },
    { type: 'command', id: 'edit.paste' },
    ...(clip.link_id
      ? ([{
          type: 'action' as const,
          label: 'Untie from its picture / sound',
          onSelect: async () => {
            await api.timeline.untieClip(project.id, clip.id)
            onChanged()
          },
        }] satisfies MenuItem[])
      : tieCandidates(clip).length
        ? ([{
            type: 'submenu' as const,
            label: 'Tie to…',
            items: tieCandidates(clip).map((other) => ({
              type: 'action' as const,
              label: `${other.clip.name || other.clip.id} (${other.track.name})`,
              onSelect: async () => {
                await api.timeline.tieClips(project.id, clip.id, other.clip.id)
                onChanged()
              },
            })),
          }] satisfies MenuItem[])
        : []),
    ...(sourceLength(clip) !== null
      ? ([{
          type: 'action' as const,
          label: `Fit to source (${sourceLength(clip)!.toFixed(2)}s)`,
          disabled: Math.abs(sourceLength(clip)! - clip.duration) < 0.01,
          onSelect: async () => {
            await api.timeline.updateClip(project.id, track.id, clip.id, {
              duration: sourceLength(clip)!,
            })
            onChanged()
          },
        }] satisfies MenuItem[])
      : []),
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
    ...(range && range.to > range.from
      ? ([
          {
            type: 'header' as const,
            label: `${(range.to - range.from).toFixed(2)}s selected`,
          },
          {
            type: 'action' as const,
            label: 'Ripple delete (every track)',
            onSelect: () => void rippleDelete(),
          },
          {
            type: 'action' as const,
            label: `Ripple delete on ${track.name}`,
            onSelect: () => void rippleDelete(track.id),
          },
          { type: 'action' as const, label: 'Clear selection', onSelect: () => setRange(null) },
          { type: 'separator' as const },
        ] satisfies MenuItem[])
      : []),
    { type: 'header', label: track.name },
    { type: 'command', id: 'edit.paste' },
    ...trackMenu(track).slice(1),
  ]
  const scrollRef = useRef<HTMLDivElement>(null)
  const width = duration * zoom

  const clipStatus = useMemo(() => {
    const map = new Map<string, ClipStatus>()
    for (const entry of resolved?.clips ?? []) {
      map.set(entry.clip_id, {
        error: entry.error,
        kind: entry.kind,
        thumb: entry.artifacts[0]?.thumb ?? null,
        // The file itself, which is what a waveform is drawn from — a thumbnail cannot show sound.
        audioPath: entry.artifacts[0]?.path ?? null,
        meta: entry.artifacts[0]?.meta ?? {},
        frames: entry.artifacts.length,
      })
    }
    return map
  }, [resolved])

  /** Time under a pointer, in seconds, accounting for how far the lanes are scrolled. */
  const timeAt = (clientX: number, element: HTMLElement) => {
    const rect = element.getBoundingClientRect()
    return Math.max(0, (clientX - rect.left + (scrollRef.current?.scrollLeft ?? 0)) / zoom)
  }

  const scrub = (event: React.MouseEvent) =>
    onScrub(timeAt(event.clientX, event.currentTarget as HTMLElement))

  /**
   * Press-and-drag scrubbing.
   *
   * Listeners go on the window rather than the ruler: the pointer routinely leaves a 28px-tall strip
   * while dragging, and a drag that stops working the moment you stray off the element is worse than no
   * drag at all.
   */
  const beginScrub = (event: React.MouseEvent) => {
    const element = event.currentTarget as HTMLElement
    onScrub(timeAt(event.clientX, element))
    const move = (e: MouseEvent) => onScrub(timeAt(e.clientX, element))
    const up = () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  /**
   * Dropping a shot from the source list onto a lane.
   *
   * It lands where it was released rather than at the end, because the position is the whole reason to
   * drag it rather than press the button in the list. The backend places the shot's audio too, so a shot
   * with sound arrives complete rather than needing the audio found and placed by hand.
   */
  const dropShot = async (event: React.DragEvent, track: Track) => {
    setDropTrack(null)
    const payload = readDrag(event)
    if (!payload || payload.kind !== 'shot' || track.locked) return
    event.preventDefault()

    const box = event.currentTarget.getBoundingClientRect()
    const at = Math.max(0, (event.clientX - box.left) / zoom)
    try {
      await api.timeline.fromShot(project.id, {
        shot_id: payload.id,
        track_id: track.kind === 'video' ? track.id : undefined,
        start: at,
      })
      onChanged()
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    }
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
          <Ruler
            duration={duration}
            zoom={zoom}
            fps={project.timeline.fps}
            playhead={playhead}
            onScrub={scrub}
            onBeginScrub={beginScrub}
          />

          <div className="relative">
            {project.timeline.tracks.map((track) => (
              <div
                key={track.id}
                className={cx(
                  'relative border-b border-[var(--color-edge)]',
                  dropTrack === track.id && 'bg-[var(--color-accent)]/15',
                )}
                style={{ height: TRACK_HEIGHT }}
                onContextMenu={(event) => onContextMenu(event, laneMenu(track))}
                onDragOver={(event) => {
                  if (!isOurDrag(event) || track.locked) return
                  event.preventDefault()
                  event.dataTransfer.dropEffect = 'copy'
                  setDropTrack(track.id)
                }}
                onDragLeave={() => setDropTrack(null)}
                onDrop={(event) => void dropShot(event, track)}
                onMouseDown={beginRange}
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
                    snapping={snapping}
                    playhead={playhead}
                    onSnapGuide={setSnapGuide}
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

            {/* The span selected in empty space, and the ripple that removes it. */}
            {range && (
              <div
                className="pointer-events-none absolute inset-y-0 border-x border-[var(--color-warn)] bg-[var(--color-warn)]/20"
                style={{ left: range.from * zoom, width: Math.max(1, (range.to - range.from) * zoom) }}
              />
            )}

            {/* Where a drag has snapped to, so the alignment can be seen as it happens. */}
            {snapGuide !== null && (
              <div
                className="pointer-events-none absolute inset-y-0 w-px bg-[var(--color-warn)]"
                style={{ left: snapGuide * zoom }}
              />
            )}

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
  duration, zoom, fps, playhead, onScrub, onBeginScrub,
}: {
  duration: number
  zoom: number
  fps: number
  playhead: number
  onScrub: (e: React.MouseEvent) => void
  onBeginScrub: (e: React.MouseEvent) => void
}) {
  // Choose a tick interval that keeps labels ~60px apart at any zoom.
  const candidates = [0.5, 1, 2, 5, 10, 30, 60, 120]
  const interval = candidates.find((c) => c * zoom >= 60) ?? 300
  const ticks = Math.ceil(duration / interval)

  return (
    <div
      className="relative h-7 cursor-ew-resize select-none border-b border-[var(--color-edge)] bg-[var(--color-panel-2)]"
      onClick={onScrub}
      onMouseDown={onBeginScrub}
    >
      {Array.from({ length: ticks + 1 }, (_, i) => (
        <div key={i} className="absolute top-0 h-full" style={{ left: i * interval * zoom }}>
          <div className="h-2 w-px bg-[var(--color-edge)]" />
          <span className="ml-1 text-[9px] text-[var(--color-ink-dim)]">
            {formatTimecode(i * interval, fps)}
          </span>
        </div>
      ))}

      {/* The grab handle. Wide enough to hit, and it sits above the ticks so it is always reachable. */}
      <div
        className="pointer-events-none absolute -top-px z-10 -ml-[6px] h-full"
        style={{ left: playhead * zoom }}
      >
        <div className="h-2.5 w-3 rounded-b-sm bg-[var(--color-accent)]" />
        <div className="ml-[5px] h-[calc(100%-0.625rem)] w-px bg-[var(--color-accent)]" />
      </div>
    </div>
  )
}

function TrackHeader({
  project, track, onChanged,
}: { project: Project; track: Track; onChanged: () => void }) {
  // An audio track silenced by someone else's solo has to look different from one you muted yourself.
  const silencedBySolo =
    !track.solo &&
    project.timeline.tracks.some((other) => other.kind === 'audio' && other.solo)

  // An audio track carries a mixer strip, which does not fit beside the name — so it goes under it.
  if (track.kind === 'audio') {
    return (
      <div
        className="flex flex-col justify-center gap-1 border-b border-[var(--color-edge)] px-2"
        style={{ height: TRACK_HEIGHT }}
      >
        <div className="flex items-center gap-1">
          <span className="min-w-0 flex-1 truncate text-xs">{track.name}</span>
          <button
            title="Delete track"
            className="px-1 text-[10px] text-[var(--color-ink-dim)] hover:text-[var(--color-bad)]"
            onClick={async () => {
              if (!confirm(`Delete track “${track.name}” and its clips?`)) return
              await api.timeline.removeTrack(project.id, track.id)
              onChanged()
            }}
          >
            ✕
          </button>
        </div>
        <TrackMixer
          project={project}
          track={track}
          silencedBySolo={silencedBySolo}
          onChanged={onChanged}
        />
      </div>
    )
  }

  return (
    <div
      className="flex items-center gap-1 border-b border-[var(--color-edge)] px-2"
      style={{ height: TRACK_HEIGHT }}
    >
      <div className="min-w-0 flex-1 overflow-hidden">
        <div className="truncate text-xs">{track.name}</div>
        <div className="truncate text-[10px] text-[var(--color-ink-dim)]">{track.kind}</div>
      </div>
      {(
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
      )}
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
  project, track, clip, zoom, status, selected, snapping, playhead, onSnapGuide,
  onSelect, onChanged, onContextMenu,
}: {
  project: Project
  track: Track
  clip: Clip
  zoom: number
  status: ClipStatus | undefined
  selected: boolean
  snapping: boolean
  playhead: number
  onSnapGuide: (time: number | null) => void
  onSelect: () => void
  onChanged: () => void
  onContextMenu: (event: React.MouseEvent) => void
}) {
  const dragState = useRef<{ mode: 'move' | 'trim'; x: number; start: number; duration: number } | null>(null)

  const beginDrag = (event: React.MouseEvent, mode: 'move' | 'trim') => {
    event.stopPropagation()
    if (track.locked) return
    dragState.current = { mode, x: event.clientX, start: clip.start, duration: clip.duration }
    const targets = snapTargets(project.timeline, playhead, clip.id)

    /** Where this drag would put the clip, snapped. Alt holds it exactly where the pointer is. */
    const resolve = (move: MouseEvent) => {
      const state = dragState.current!
      const delta = (move.clientX - state.x) / zoom
      const on = snapping && !move.altKey
      if (state.mode === 'move') {
        const wanted = Math.max(0, state.start + delta)
        const result = snapMove(wanted, state.duration, targets, zoom, on)
        return { start: Math.max(0, result.time), duration: state.duration, guide: result.target }
      }
      const wantedEnd = state.start + Math.max(0.04, state.duration + delta)
      const result = snap(wantedEnd, targets, zoom, on)
      return {
        start: state.start,
        duration: Math.max(0.04, result.time - state.start),
        guide: result.target,
      }
    }

    // Tied clips move with it as you drag, not only once the server has been told — otherwise the
    // partner sits still and then jumps, which reads as a glitch rather than as a tie.
    const partners = clip.link_id
      ? project.timeline.tracks.flatMap((other) =>
          other.clips
            .filter((c) => c.link_id === clip.link_id && c.id !== clip.id)
            .map((c) => ({ id: c.id, start: c.start, duration: c.duration })),
        )
      : []

    const onMove = (move: MouseEvent) => {
      if (!dragState.current) return
      const element = document.getElementById(`clip-${clip.id}`)
      if (!element) return
      const state = dragState.current
      const { start, duration, guide } = resolve(move)
      element.style.left = `${start * zoom}px`
      element.style.width = `${duration * zoom}px`

      for (const partner of partners) {
        const node = document.getElementById(`clip-${partner.id}`)
        if (!node) continue
        node.style.left = `${Math.max(0, partner.start + (start - state.start)) * zoom}px`
        node.style.width = `${Math.max(0.04, partner.duration + (duration - state.duration)) * zoom}px`
      }
      onSnapGuide(guide ? guide.time : null)
    }

    const onUp = async (up: MouseEvent) => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      const state = dragState.current
      dragState.current = null
      onSnapGuide(null)
      if (!state) return

      if (Math.abs(up.clientX - state.x) < 2) return
      const { start, duration } = resolve(up)
      // Persist once, on release — dragging fires far too often to PATCH every frame.
      const patch = state.mode === 'move' ? { start } : { duration }
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
      {/* Audio is drawn as its own shape; a thumbnail would say nothing about where the sound is. */}
      {track.kind === 'audio' && status?.audioPath ? (
        <Waveform
          projectId={project.id}
          path={status.audioPath}
          inPoint={clip.in_point}
          duration={clip.duration}
          className="absolute inset-0 h-full w-full text-[var(--color-accent)]"
        />
      ) : status?.thumb ? (
        <img
          src={api.media.url(project.id, status.thumb)}
          alt=""
          className="h-full w-10 shrink-0 object-cover opacity-80"
        />
      ) : null}
      <span className="relative truncate px-1.5 text-[10px]">
        {status?.error ? '⚠ ' : ''}
        {clip.link_id && (
          <span title="Tied to another clip — they move and trim together" className="opacity-70">⛓ </span>
        )}
        {clip.name || clip.text || clip.source.port_key || 'clip'}
      </span>
      <div
        onMouseDown={(e) => beginDrag(e, 'trim')}
        className="absolute right-0 top-0 h-full w-1.5 cursor-ew-resize bg-white/20 hover:bg-white/40"
      />
    </div>
  )
}

// -- clip inspector -------------------------------------------------------------------------------------

export function ClipInspector({ project, onChanged }: { project: Project; onChanged: () => void }) {
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

  /**
   * The other outputs of the step this clip came from.
   *
   * A step can produce several — an image and the caption that made it, a video and its audio — and the
   * clip was placed on whichever one was found first. Which of them the timeline should show is an
   * editing decision, so it belongs here rather than being fixed when the clip was created.
   */
  const sourceStep = clip.source.kind === 'step_output'
    ? project.shots
        .find((shot) => shot.id === clip.source.shot_id)
        ?.steps.find((step) => step.id === clip.source.step_id)
    : undefined
  const outputs = sourceStep
    ? (project.workflows[sourceStep.workflow_id]?.ports ?? []).filter((p) => p.direction === 'out')
    : []

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
        {/* Only worth showing when there is a choice to make. */}
        {outputs.length > 1 && (
          <div className="col-span-4">
            <Field
              label="Output"
              hint={`${sourceStep?.name ?? 'this step'} produces ${outputs.length}`}
            >
              <Select
                value={clip.source.port_key ?? ''}
                onChange={(e) =>
                  patch({ source: { ...clip.source, port_key: e.target.value } })
                }
              >
                {outputs.map((port) => (
                  <option key={port.key} value={port.key}>
                    {port.label || port.key} ({port.kind})
                    {(TRACK_ACCEPTS[track.kind] ?? []).includes(port.kind)
                      ? ''
                      : ` — a ${track.kind} track cannot show this`}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
        )}
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
        {/* Audio clips have no picture to fade or fit, so they get the controls they do have. */}
        {track.kind === 'audio' ? (
          <>
            <Field label="Volume" hint={`${Math.round(clip.volume * 100)}%`}>
              <input
                type="range" min={0} max={2} step={0.01} value={clip.volume}
                onChange={(e) => patch({ volume: Number(e.target.value) })}
                className="w-full accent-[var(--color-accent)]"
              />
            </Field>
            <Field
              label="Pan"
              hint={clip.pan === 0
                ? 'centre'
                : `${Math.abs(Math.round(clip.pan * 100))}% ${clip.pan < 0 ? 'left' : 'right'}`}
            >
              <input
                type="range" min={-1} max={1} step={0.01} value={clip.pan}
                onChange={(e) => patch({ pan: Number(e.target.value) })}
                onDoubleClick={() => patch({ pan: 0 })}
                className="w-full accent-[var(--color-accent)]"
              />
            </Field>
          </>
        ) : (
          <>
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
          </>
        )}

        {track.kind === 'text' && (
          <div className="col-span-4">
            <Field label="Text">
              <TextInput value={clip.text} onChange={(e) => patch({ text: e.target.value })} />
            </Field>
          </div>
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
