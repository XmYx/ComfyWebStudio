/**
 * Choosing what to render and how.
 *
 * Scope first, because it is the decision that changes what comes out: the whole cut, a span between two
 * times, the selected clip on its own, or every clip as its own file. Output settings sit underneath and
 * default to the project's — an untouched field renders exactly as the Render button always did, and
 * nothing here writes back to the project.
 */

import { useEffect, useMemo, useState } from 'react'

import type { Clip, Project, RenderRequest, RenderScope } from '@/api/types'
import { formatTimecode } from '@/lib/format'
import { Button, Callout, Field, Modal, Select, TextInput, cx } from '@/components/ui'

interface Props {
  open: boolean
  onClose: () => void
  project: Project
  /** Where the playhead is, used for the still and to seed the range. */
  playhead: number
  selectedClip: { track: string; clip: Clip } | null
  onRender: (body: RenderRequest) => void
}

const CONTAINERS = ['mp4', 'mov', 'mkv', 'webm'] as const
const CODECS = [
  { value: 'libx264', label: 'H.264 (libx264)' },
  { value: 'libx265', label: 'H.265 (libx265)' },
  { value: 'libvpx-vp9', label: 'VP9 (libvpx-vp9)' },
  { value: 'prores_ks', label: 'ProRes' },
] as const

/** Quality presets, so the common case is not "what is a sensible CRF". */
const QUALITY = [
  { value: 14, label: 'Very high' },
  { value: 18, label: 'High' },
  { value: 23, label: 'Medium' },
  { value: 28, label: 'Small file' },
] as const

export function RenderDialog({ open, onClose, project, playhead, selectedClip, onRender }: Props) {
  const timeline = project.timeline
  const clipCount = useMemo(
    () =>
      timeline.tracks
        .filter((t) => !t.muted)
        .reduce((total, track) => total + track.clips.filter((c) => c.enabled).length, 0),
    [timeline.tracks],
  )

  const [scope, setScope] = useState<RenderScope>('timeline')
  const [name, setName] = useState('')
  const [still, setStill] = useState(false)
  const [start, setStart] = useState(0)
  const [end, setEnd] = useState(timeline.duration)
  const [width, setWidth] = useState(timeline.width)
  const [height, setHeight] = useState(timeline.height)
  const [fps, setFps] = useState(timeline.fps)
  const [container, setContainer] = useState<string>('mp4')
  const [codec, setCodec] = useState<string>('libx264')
  const [crf, setCrf] = useState<number>(18)

  // Reopening should reflect the timeline as it is now, and default to the clip if one is selected —
  // that is almost always why someone opens this with a clip highlighted.
  useEffect(() => {
    if (!open) return
    setScope(selectedClip ? 'clip' : 'timeline')
    setStart(0)
    setEnd(timeline.duration)
    setWidth(timeline.width)
    setHeight(timeline.height)
    setFps(timeline.fps)
    setStill(false)
  }, [open])  // eslint-disable-line react-hooks/exhaustive-deps

  const scopes: Array<{ value: RenderScope; label: string; hint: string; disabled?: boolean }> = [
    {
      value: 'timeline',
      label: 'Whole timeline',
      hint: formatTimecode(timeline.duration, timeline.fps),
    },
    { value: 'range', label: 'Range', hint: 'between an in and an out point' },
    {
      value: 'clip',
      label: 'Selected clip',
      hint: selectedClip ? selectedClip.clip.name || 'the highlighted clip' : 'select a clip first',
      disabled: !selectedClip,
    },
    {
      value: 'clips',
      label: 'Each clip separately',
      hint: `${clipCount} file${clipCount === 1 ? '' : 's'}`,
      disabled: clipCount === 0,
    },
  ]

  const rangeInvalid = scope === 'range' && end - start <= 0
  const submit = () => {
    const body: RenderRequest = {
      scope,
      name: name.trim() || undefined,
      still,
      time_s: playhead,
      width,
      height,
      fps,
      ...(scope === 'range' ? { start_s: start, end_s: end } : {}),
      ...(scope === 'clip' && selectedClip ? { clip_id: selectedClip.clip.id } : {}),
      ...(still ? {} : { container, video_codec: codec, crf }),
    }
    onRender(body)
    onClose()
  }

  return (
    <Modal open={open} onClose={onClose} title="Render" width="max-w-xl">
      <div className="space-y-4">
        <div>
          <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-[var(--color-ink-dim)]">
            What to render
          </div>
          <div className="grid grid-cols-2 gap-1.5">
            {scopes.map((option) => (
              <button
                key={option.value}
                disabled={option.disabled}
                onClick={() => setScope(option.value)}
                className={cx(
                  'rounded-md border px-2.5 py-2 text-left transition-colors',
                  scope === option.value
                    ? 'border-[var(--color-accent)] bg-[var(--color-accent)]/10'
                    : 'border-[var(--color-edge)] hover:bg-[var(--color-panel-2)]',
                  option.disabled && 'cursor-not-allowed opacity-40',
                )}
              >
                <div className="text-xs font-medium">{option.label}</div>
                <div className="truncate text-[10px] text-[var(--color-ink-dim)]">{option.hint}</div>
              </button>
            ))}
          </div>
        </div>

        {scope === 'range' && (
          <div className="grid grid-cols-3 gap-3">
            <Field label="In (s)">
              <TextInput
                type="number" step={0.1} min={0} value={start}
                onChange={(e) => setStart(Math.max(0, Number(e.target.value)))}
              />
            </Field>
            <Field label="Out (s)">
              <TextInput
                type="number" step={0.1} min={0} value={end}
                onChange={(e) => setEnd(Number(e.target.value))}
              />
            </Field>
            <div className="flex items-end">
              <Button
                size="sm"
                variant="ghost"
                title="Set the out point to the playhead"
                onClick={() => setEnd(Number(playhead.toFixed(3)))}
              >
                Out = playhead
              </Button>
            </div>
          </div>
        )}

        <label className="flex cursor-pointer items-center gap-2 text-xs">
          <input
            type="checkbox"
            checked={still}
            onChange={(e) => setStill(e.target.checked)}
            className="size-4 accent-[var(--color-accent)]"
          />
          <span>
            Single frame at the playhead ({formatTimecode(playhead, timeline.fps)})
            <span className="ml-1 text-[var(--color-ink-dim)]">— a PNG rather than a movie</span>
          </span>
        </label>

        <div>
          <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-[var(--color-ink-dim)]">
            Output
          </div>
          <div className="grid grid-cols-3 gap-3">
            <Field label="Width">
              <TextInput
                type="number" min={16} step={2} value={width}
                onChange={(e) => setWidth(Number(e.target.value))}
              />
            </Field>
            <Field label="Height">
              <TextInput
                type="number" min={16} step={2} value={height}
                onChange={(e) => setHeight(Number(e.target.value))}
              />
            </Field>
            <Field label="FPS">
              <TextInput
                type="number" min={1} step={1} value={fps}
                onChange={(e) => setFps(Number(e.target.value))}
              />
            </Field>
          </div>

          {!still && (
            <div className="mt-3 grid grid-cols-3 gap-3">
              <Field label="Container">
                <Select value={container} onChange={(e) => setContainer(e.target.value)}>
                  {CONTAINERS.map((value) => (
                    <option key={value} value={value}>{value}</option>
                  ))}
                </Select>
              </Field>
              <Field label="Codec">
                <Select value={codec} onChange={(e) => setCodec(e.target.value)}>
                  {CODECS.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </Select>
              </Field>
              <Field label="Quality">
                <Select value={String(crf)} onChange={(e) => setCrf(Number(e.target.value))}>
                  {QUALITY.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </Select>
              </Field>
            </div>
          )}
        </div>

        <Field label="File name" hint="a name is generated when this is left empty">
          <TextInput
            value={name}
            placeholder={`${project.name}-…`}
            onChange={(e) => setName(e.target.value)}
          />
        </Field>

        {rangeInvalid && (
          <Callout tone="bad">The out point has to come after the in point.</Callout>
        )}
        {scope === 'clips' && still && (
          <Callout tone="info">
            One PNG per clip, each taken from that clip's own start.
          </Callout>
        )}

        <div className="flex justify-end gap-2 border-t border-[var(--color-edge)] pt-3">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={rangeInvalid} onClick={submit}>
            {scope === 'clips' ? `Render ${clipCount} file${clipCount === 1 ? '' : 's'}` : 'Render'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
