/**
 * Choosing what to render and how.
 *
 * Scope first, because it is the decision that changes what comes out: the whole cut, a span between two
 * times, the selected clip on its own, or every clip as its own file.
 *
 * Output settings sit underneath, and they *are* remembered: the dialog reopens on whatever this project
 * last rendered with, because a portrait short and a 4K landscape piece live side by side and retyping
 * four fields every time is not a decision anybody is making afresh. Presets are the same idea one level
 * up — a named size and format, editable and deletable whichever of them shipped with the app.
 *
 * The composition size on the timeline is left alone throughout. Rendering a 1024×1024 cut out at 1080p
 * is a legitimate thing to do and must not resize the project to match.
 */

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { Clip, Project, RenderPreset, RenderRequest, RenderScope } from '@/api/types'
import { formatTimecode } from '@/lib/format'
import { Button, Callout, Field, Modal, Select, TextInput, cx, useToast } from '@/components/ui'

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
  const [presetId, setPresetId] = useState<string>('')

  const toast = useToast()
  const queryClient = useQueryClient()
  const { data: presets } = useQuery({
    queryKey: ['render-presets'],
    queryFn: () => api.settings.renderPresets(),
    enabled: open,
  })

  const refreshPresets = () => queryClient.invalidateQueries({ queryKey: ['render-presets'] })
  const fail = (error: unknown) => toast.push('bad', (error as ApiError).message)

  // Reopening picks up where the last render left off, falling back to the timeline for a project that
  // has never been rendered. Scope defaults to the clip when one is selected — that is almost always why
  // someone opens this with a clip highlighted.
  useEffect(() => {
    if (!open) return
    const last = project.settings.render
    setScope(selectedClip ? 'clip' : 'timeline')
    setStart(0)
    setEnd(timeline.duration)
    setWidth(last?.width ?? timeline.width)
    setHeight(last?.height ?? timeline.height)
    setFps(last?.fps ?? timeline.fps)
    setContainer(last?.container ?? 'mp4')
    setCodec(last?.video_codec ?? 'libx264')
    setCrf(last?.crf ?? 18)
    setPresetId(last?.preset_id ?? '')
    setStill(false)
  }, [open])  // eslint-disable-line react-hooks/exhaustive-deps

  /** Everything a preset carries, so "does this still match?" is asked in one place. */
  const matches = (preset: RenderPreset) =>
    preset.width === width &&
    preset.height === height &&
    preset.container === container &&
    preset.video_codec === codec &&
    preset.crf === crf &&
    (preset.fps == null || preset.fps === fps)

  const selectedPreset = presets?.find((p) => p.id === presetId) ?? null
  // Shown as selected only while the settings still are what it says. Editing a field by hand and
  // leaving the preset's name in the box would make the box a lie.
  const activePreset = selectedPreset && matches(selectedPreset) ? selectedPreset : null

  const applyPreset = (preset: RenderPreset) => {
    setWidth(preset.width)
    setHeight(preset.height)
    if (preset.fps != null) setFps(preset.fps)
    setContainer(preset.container)
    setCodec(preset.video_codec)
    setCrf(preset.crf)
    setPresetId(preset.id)
  }

  const asPreset = () => ({ width, height, fps, container, video_codec: codec, crf })

  const savePreset = useMutation({
    mutationFn: async () => {
      const name = window.prompt('Name this preset', `${width}×${height}`)?.trim()
      if (!name) return null
      return api.settings.addRenderPreset({ name, ...asPreset() })
    },
    onSuccess: (created) => {
      if (!created) return
      setPresetId(created.id)
      refreshPresets()
      toast.push('ok', `Saved “${created.name}”.`)
    },
    onError: fail,
  })

  const updatePreset = useMutation({
    mutationFn: () => api.settings.updateRenderPreset(selectedPreset!.id, asPreset()),
    onSuccess: (updated) => {
      refreshPresets()
      toast.push('ok', `Updated “${updated.name}”.`)
    },
    onError: fail,
  })

  const deletePreset = useMutation({
    mutationFn: async () => {
      if (!window.confirm(`Delete the preset “${selectedPreset!.name}”?`)) return false
      await api.settings.removeRenderPreset(selectedPreset!.id)
      return true
    },
    onSuccess: (removed) => {
      if (!removed) return
      setPresetId('')
      refreshPresets()
    },
    onError: fail,
  })

  const restorePresets = useMutation({
    mutationFn: () => api.settings.restoreRenderPresets(),
    onSuccess: (all) => {
      refreshPresets()
      toast.push('ok', `${all.length} presets available.`)
    },
    onError: fail,
  })

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
      preset_id: activePreset?.id ?? null,
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
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-[var(--color-ink-dim)]">
              Output
            </span>
            <button
              title="Swap width and height"
              onClick={() => { setWidth(height); setHeight(width) }}
              className="text-[10px] text-[var(--color-ink-dim)] hover:text-[var(--color-ink)]"
            >
              ⇄ {width >= height ? 'landscape' : 'portrait'}
            </button>
          </div>

          <div className="mb-3 flex items-end gap-2">
            <div className="min-w-0 flex-1">
              <Field label="Preset">
                <Select
                  value={activePreset?.id ?? ''}
                  onChange={(e) => {
                    const found = presets?.find((p) => p.id === e.target.value)
                    if (found) applyPreset(found)
                    else setPresetId('')
                  }}
                >
                  <option value="">
                    {selectedPreset ? `${selectedPreset.name} (edited)` : 'Custom'}
                  </option>
                  {(presets ?? []).map((preset) => (
                    <option key={preset.id} value={preset.id}>
                      {preset.name} — {preset.width}×{preset.height}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <Button size="sm" variant="ghost" title="Save these settings as a new preset"
                    onClick={() => savePreset.mutate()}>
              Save as…
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={!selectedPreset}
              title={selectedPreset ? `Overwrite “${selectedPreset.name}”` : 'Pick a preset to update'}
              onClick={() => updatePreset.mutate()}
            >
              Update
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={!selectedPreset}
              title={selectedPreset ? `Delete “${selectedPreset.name}”` : 'Pick a preset to delete'}
              onClick={() => deletePreset.mutate()}
            >
              ✕
            </Button>
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

        <button
          onClick={() => restorePresets.mutate()}
          className="text-[10px] text-[var(--color-ink-dim)] underline-offset-2 hover:underline"
        >
          Restore the built-in presets
        </button>

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
