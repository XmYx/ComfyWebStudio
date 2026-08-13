import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { AppSettings, BackendConfig, BackendStatus } from '@/api/types'
import { formatBytes } from '@/lib/format'
import {
  Badge, Button, Callout, Checkbox, Field, Modal, Panel, PanelHeader,
  Select, Spinner, TextInput, cx, useToast,
} from '@/components/ui'

type Section =
  | 'backends' | 'models' | 'flow' | 'paths' | 'execution' | 'render' | 'preview' | 'ui'

import { LlmSettings } from './LlmSettings'
import { PipelineSettings } from './PipelineSettings'

const SECTIONS: Array<[Section, string]> = [
  ['backends', 'ComfyUI backends'],
  ['models', 'Language models'],
  ['flow', 'Storyboard flow'],
  ['paths', 'Paths'],
  ['execution', 'Execution'],
  ['render', 'Render'],
  ['preview', 'Preview'],
  ['ui', 'Interface'],
]

export function SettingsPage() {
  const [section, setSection] = useState<Section>('backends')
  const queryClient = useQueryClient()
  const toast = useToast()

  const { data: settings, isLoading } = useQuery({
    queryKey: ['settings'],
    queryFn: api.settings.get,
  })

  const save = useMutation({
    mutationFn: (patch: Partial<AppSettings>) => api.settings.update(patch),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings'] })
      toast.push('ok', 'Settings saved.')
    },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  if (isLoading || !settings) return <div className="p-6 text-xs text-[var(--color-ink-dim)]">Loading…</div>

  return (
    <div className="mx-auto grid h-full max-w-5xl grid-cols-[180px_1fr] gap-4 p-6">
      <nav className="space-y-0.5">
        {SECTIONS.map(([key, label]) => (
          <button
            key={key}
            onClick={() => setSection(key)}
            className={cx(
              'w-full rounded-md px-3 py-1.5 text-left text-sm transition-colors',
              section === key
                ? 'bg-[var(--color-panel-2)] text-[var(--color-ink)]'
                : 'text-[var(--color-ink-dim)] hover:text-[var(--color-ink)]',
            )}
          >
            {label}
          </button>
        ))}
      </nav>

      <div className="min-h-0 overflow-y-auto">
        {section === 'backends' && <BackendsSection settings={settings} />}
        {section === 'models' && (
          <LlmSettings
            settings={settings}
            onChanged={() => queryClient.invalidateQueries({ queryKey: ['settings'] })}
          />
        )}
        {section === 'flow' && <PipelineSettings />}
        {section === 'paths' && <PathsSection settings={settings} onSave={save.mutate} />}
        {section === 'execution' && <ExecutionSection settings={settings} onSave={save.mutate} />}
        {section === 'render' && <RenderSection settings={settings} onSave={save.mutate} />}
        {section === 'preview' && <PreviewSection settings={settings} onSave={save.mutate} />}
        {section === 'ui' && <UISection settings={settings} onSave={save.mutate} />}
      </div>
    </div>
  )
}

// -- backends -------------------------------------------------------------------------------------------

function BackendsSection({ settings }: { settings: AppSettings }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [editing, setEditing] = useState<Partial<BackendConfig> | null>(null)
  const [statuses, setStatuses] = useState<Record<string, BackendStatus | 'testing'>>({})

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['settings'] })

  const test = async (id: string) => {
    setStatuses((s) => ({ ...s, [id]: 'testing' }))
    try {
      const result = await api.settings.testBackend(id)
      setStatuses((s) => ({ ...s, [id]: result }))
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
      setStatuses((s) => {
        const next = { ...s }
        delete next[id]
        return next
      })
    }
  }

  return (
    <div className="space-y-3">
      <Panel>
        <PanelHeader
          actions={
            <Button
              size="sm"
              variant="primary"
              onClick={() => setEditing({ name: 'ComfyUI', kind: 'local', base_url: 'http://127.0.0.1:8188' })}
            >
              + Add
            </Button>
          }
        >
          ComfyUI backends
        </PanelHeader>

        <div className="space-y-2 p-3">
          {!settings.backends.length && (
            <Callout tone="warn" title="No backend configured">
              ComfyWebStudio needs at least one ComfyUI instance to run anything.
            </Callout>
          )}

          {settings.backends.map((backend) => {
            const status = statuses[backend.id]
            return (
              <div key={backend.id} className="rounded-md border border-[var(--color-edge)] p-3">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">{backend.name}</span>
                  <Badge tone={backend.kind === 'local' ? 'info' : 'muted'}>{backend.kind}</Badge>
                  {settings.default_backend_id === backend.id && <Badge tone="ok">default</Badge>}
                  {!backend.enabled && <Badge tone="warn">disabled</Badge>}
                  <div className="flex-1" />
                  <Button size="sm" variant="ghost" onClick={() => test(backend.id)}>
                    {status === 'testing' ? <Spinner /> : null} Test
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setEditing(backend)}>Edit</Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={async () => {
                      if (!confirm(`Remove backend “${backend.name}”?`)) return
                      await api.settings.removeBackend(backend.id)
                      refresh()
                    }}
                  >
                    ✕
                  </Button>
                </div>

                <div className="mt-1 font-mono text-[11px] text-[var(--color-ink-dim)]">
                  {backend.base_url}
                  {backend.comfy_root && ` · ${backend.comfy_root}`}
                </div>

                {status && status !== 'testing' && (
                  <div className="mt-2 space-y-1 rounded bg-[var(--color-surface)] p-2 text-[11px]">
                    {status.reachable ? (
                      <>
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge tone="ok">reachable</Badge>
                          <span>ComfyUI {status.comfyui_version}</span>
                          {status.shared_filesystem && (
                            <Badge tone="info" title="Chaining is zero-copy on a shared filesystem">
                              shared filesystem
                            </Badge>
                          )}
                        </div>
                        <div>
                          {status.node_pack ? (
                            <Badge tone={status.protocol_ok ? 'ok' : 'warn'}>
                              node pack {status.node_pack.pack_version}
                              {!status.protocol_ok && ' · protocol mismatch'}
                            </Badge>
                          ) : (
                            <div className="flex items-center gap-2">
                              <Badge tone="bad">node pack missing</Badge>
                              {backend.kind === 'local' && backend.comfy_root && (
                                <Button
                                  size="sm"
                                  onClick={async () => {
                                    try {
                                      const result = await api.settings.installNodepack(backend.id)
                                      toast.push('ok', result.message ?? `Linked at ${result.path}`)
                                    } catch (error) {
                                      toast.push('bad', (error as ApiError).message)
                                    }
                                  }}
                                >
                                  Install
                                </Button>
                              )}
                            </div>
                          )}
                        </div>
                        {status.devices?.map((device) => (
                          <div key={device.name} className="text-[var(--color-ink-dim)]">
                            {device.name}
                            {device.vram_total
                              ? ` · ${formatBytes(device.vram_free ?? 0)} free of ${formatBytes(device.vram_total)}`
                              : ''}
                          </div>
                        ))}
                      </>
                    ) : (
                      <Callout tone="bad">{status.error}</Callout>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </Panel>

      <BackendModal
        backend={editing}
        onClose={() => setEditing(null)}
        onSaved={() => { setEditing(null); refresh() }}
      />
    </div>
  )
}

function BackendModal({
  backend, onClose, onSaved,
}: { backend: Partial<BackendConfig> | null; onClose: () => void; onSaved: () => void }) {
  const toast = useToast()
  const [draft, setDraft] = useState<Partial<BackendConfig>>({})
  const [initialised, setInitialised] = useState<string | null>(null)

  // Adopt the record being edited exactly once per open, so typing is not overwritten on re-render.
  if (backend && initialised !== (backend.id ?? 'new')) {
    setDraft({ ...backend })
    setInitialised(backend.id ?? 'new')
  }
  if (!backend && initialised !== null) setInitialised(null)

  const set = (patch: Partial<BackendConfig>) => setDraft((d) => ({ ...d, ...patch }))

  const save = async () => {
    try {
      const body = {
        name: draft.name ?? 'ComfyUI',
        kind: draft.kind ?? 'local',
        base_url: draft.base_url ?? '',
        headers: draft.headers ?? {},
        comfy_root: draft.comfy_root || null,
        comfy_user: draft.comfy_user ?? 'default',
        enabled: draft.enabled ?? true,
        timeout_s: draft.timeout_s ?? 60,
      }
      if (draft.id) await api.settings.updateBackend(draft.id, body)
      else await api.settings.addBackend(body)
      onSaved()
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    }
  }

  return (
    <Modal open={Boolean(backend)} onClose={onClose} title={draft.id ? 'Edit backend' : 'Add backend'}>
      <div className="space-y-3">
        <Field label="Name">
          <TextInput value={draft.name ?? ''} onChange={(e) => set({ name: e.target.value })} />
        </Field>

        <Field
          label="Kind"
          hint="local enables the zero-copy fast path"
        >
          <Select
            value={draft.kind ?? 'local'}
            onChange={(e) => set({ kind: e.target.value as 'local' | 'remote' })}
          >
            <option value="local">Local (same machine)</option>
            <option value="remote">Remote / cloud (API only)</option>
          </Select>
        </Field>

        <Field label="Base URL">
          <TextInput
            value={draft.base_url ?? ''}
            placeholder="http://127.0.0.1:8188"
            onChange={(e) => set({ base_url: e.target.value })}
          />
        </Field>

        {draft.kind === 'local' && (
          <Field
            label="ComfyUI directory"
            hint="optional; enables passing files by path instead of uploading"
          >
            <TextInput
              value={draft.comfy_root ?? ''}
              placeholder="/home/you/ComfyUI"
              onChange={(e) => set({ comfy_root: e.target.value })}
            />
          </Field>
        )}

        {draft.kind === 'remote' && (
          <Field label="Authorization header" hint="optional">
            <TextInput
              value={draft.headers?.Authorization ?? ''}
              placeholder="Bearer …"
              onChange={(e) => set({ headers: e.target.value ? { Authorization: e.target.value } : {} })}
            />
          </Field>
        )}

        <Checkbox
          checked={draft.enabled ?? true}
          onChange={(enabled) => set({ enabled })}
          label="Enabled"
        />

        <div className="flex justify-end gap-2 pt-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={save}>Save</Button>
        </div>
      </div>
    </Modal>
  )
}

// -- simple sections ------------------------------------------------------------------------------------

type SectionProps = { settings: AppSettings; onSave: (patch: Partial<AppSettings>) => void }

function PathsSection({ settings, onSave }: SectionProps) {
  const [projectsDir, setProjectsDir] = useState(settings.projects_dir)
  return (
    <Panel>
      <PanelHeader>Paths</PanelHeader>
      <div className="space-y-3 p-3">
        <Field label="State directory" hint="set with the CWS_ROOT environment variable">
          <TextInput value={settings.root} disabled />
        </Field>
        <Field label="Projects directory">
          <TextInput value={projectsDir} onChange={(e) => setProjectsDir(e.target.value)} />
        </Field>
        <Button
          variant="primary"
          size="sm"
          disabled={projectsDir === settings.projects_dir}
          onClick={() => onSave({ projects_dir: projectsDir } as Partial<AppSettings>)}
        >
          Save
        </Button>
      </div>
    </Panel>
  )
}

function ExecutionSection({ settings, onSave }: SectionProps) {
  const execution = settings.execution
  const patch = (body: Partial<AppSettings['execution']>) =>
    onSave({ execution: { ...execution, ...body } })

  return (
    <Panel>
      <PanelHeader>Execution</PanelHeader>
      <div className="space-y-3 p-3">
        <Field label="Max concurrent steps" hint="independent branches run in parallel">
          <TextInput
            type="number" min={1} max={16} value={execution.max_concurrent_steps}
            onChange={(e) => patch({ max_concurrent_steps: Number(e.target.value) })}
          />
        </Field>
        <Checkbox
          checked={execution.enable_cache}
          onChange={(enable_cache) => patch({ enable_cache })}
          label="Reuse results when nothing that affects a step has changed"
        />
        <Field label="Step timeout (seconds)" hint="blank for no limit">
          <TextInput
            type="number" value={execution.step_timeout_s ?? ''}
            onChange={(e) => patch({ step_timeout_s: e.target.value ? Number(e.target.value) : null })}
          />
        </Field>
        <Field label="Default seed mode">
          <Select
            value={execution.default_seed_mode}
            onChange={(e) => patch({ default_seed_mode: e.target.value as any })}
          >
            <option value="fixed">Fixed</option>
            <option value="randomize">Randomize each run</option>
            <option value="increment">Increment each run</option>
          </Select>
        </Field>
      </div>
    </Panel>
  )
}

function RenderSection({ settings, onSave }: SectionProps) {
  const render = settings.render
  const patch = (body: Partial<AppSettings['render']>) => onSave({ render: { ...render, ...body } })

  return (
    <Panel>
      <PanelHeader>Render defaults</PanelHeader>
      <div className="grid grid-cols-2 gap-3 p-3">
        <Field label="Frame rate">
          <TextInput type="number" step={0.001} value={render.fps} onChange={(e) => patch({ fps: Number(e.target.value) })} />
        </Field>
        <Field label="Container">
          <Select value={render.container} onChange={(e) => patch({ container: e.target.value })}>
            <option value="mp4">mp4</option>
            <option value="webm">webm</option>
            <option value="mkv">mkv</option>
            <option value="mov">mov</option>
          </Select>
        </Field>
        <Field label="Width">
          <TextInput type="number" value={render.width} onChange={(e) => patch({ width: Number(e.target.value) })} />
        </Field>
        <Field label="Height">
          <TextInput type="number" value={render.height} onChange={(e) => patch({ height: Number(e.target.value) })} />
        </Field>
        <Field label="Video codec">
          <Select value={render.video_codec} onChange={(e) => patch({ video_codec: e.target.value })}>
            <option value="libx264">H.264 (libx264)</option>
            <option value="libx265">H.265 (libx265)</option>
            <option value="libvpx-vp9">VP9</option>
          </Select>
        </Field>
        <Field label="Quality (CRF)" hint="lower is better">
          <TextInput type="number" min={0} max={51} value={render.crf} onChange={(e) => patch({ crf: Number(e.target.value) })} />
        </Field>
        <Field label="Pixel format">
          <TextInput value={render.pix_fmt} onChange={(e) => patch({ pix_fmt: e.target.value })} />
        </Field>
        <Field label="Audio bitrate">
          <TextInput value={render.audio_bitrate} onChange={(e) => patch({ audio_bitrate: e.target.value })} />
        </Field>
      </div>
    </Panel>
  )
}

function PreviewSection({ settings, onSave }: SectionProps) {
  const preview = settings.preview
  const patch = (body: Partial<AppSettings['preview']>) => onSave({ preview: { ...preview, ...body } })

  return (
    <Panel>
      <PanelHeader>Preview</PanelHeader>
      <div className="space-y-3 p-3">
        <Field label="Thumbnail size (px)">
          <TextInput
            type="number" min={64} max={2048} value={preview.thumbnail_size}
            onChange={(e) => patch({ thumbnail_size: Number(e.target.value) })}
          />
        </Field>
        <Field label="Thumbnail format">
          <Select value={preview.thumbnail_format} onChange={(e) => patch({ thumbnail_format: e.target.value })}>
            <option value="webp">WebP</option>
            <option value="jpeg">JPEG</option>
            <option value="png">PNG</option>
          </Select>
        </Field>
        <Checkbox
          checked={preview.autoplay_video}
          onChange={(autoplay_video) => patch({ autoplay_video })}
          label="Autoplay video previews"
        />
      </div>
    </Panel>
  )
}

function UISection({ settings, onSave }: SectionProps) {
  const ui = settings.ui
  const patch = (body: Partial<AppSettings['ui']>) => onSave({ ui: { ...ui, ...body } })

  return (
    <Panel>
      <PanelHeader>Interface</PanelHeader>
      <div className="space-y-3 p-3">
        <Checkbox
          checked={ui.timeline_snap}
          onChange={(timeline_snap) => patch({ timeline_snap })}
          label="Snap timeline edits to frames"
        />
        <Field label="Snap to every N frames">
          <TextInput
            type="number" min={1} value={ui.timeline_snap_frames}
            onChange={(e) => patch({ timeline_snap_frames: Number(e.target.value) })}
          />
        </Field>
      </div>
    </Panel>
  )
}
