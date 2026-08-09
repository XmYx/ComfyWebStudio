/**
 * The dialogs the menus open.
 *
 * All of them read `layout.dialog`, so a menu item only has to name a dialog id — it never has to know
 * where the component lives or hold its own open/close state.
 */

import { useMemo, useState } from 'react'
import { useNavigate, useMatch } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { Project } from '@/api/types'
import { useLayout } from '@/store/layout'
import { COMMANDS, MENUS, formatShortcut } from './commands'
import { HistoryDialog } from '@/features/history/HistoryDialog'
import {
  Badge, Button, Callout, Checkbox, Field, Modal, Spinner, TextArea, TextInput, cx, useToast,
} from '@/components/ui'

export function AppDialogs() {
  const dialog = useLayout((s) => s.dialog)
  const close = useLayout((s) => s.closeDialog)
  const match = useMatch('/p/:projectId/*')
  const projectId = match?.params.projectId

  const { data: project } = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => api.projects.get(projectId!),
    enabled: Boolean(projectId),
  })

  return (
    <>
      <ShortcutsDialog open={dialog === 'shortcuts'} onClose={close} />
      <AboutDialog open={dialog === 'about'} onClose={close} />
      <NewProjectDialog open={dialog === 'newProject'} onClose={close} />
      <PluginsDialog open={dialog === 'plugins'} onClose={close} project={project ?? null} />
      <BuildPluginDialog open={dialog === 'buildPlugin'} onClose={close} project={project ?? null} />
      <ExportProjectDialog open={dialog === 'exportProject'} onClose={close} project={project ?? null} />
      <HistoryDialog />
    </>
  )
}

// -- Help -----------------------------------------------------------------------------------------------

function ShortcutsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  // Group by the menu each command lives in, so the list reads the same way the menus do.
  const groups = useMemo(() => {
    return MENUS.map((menu) => {
      const ids = new Set<string>()
      const walk = (items: typeof menu.items) => {
        for (const item of items) {
          if (item.type === 'command') ids.add(item.id)
          if (item.type === 'submenu') walk(item.items)
        }
      }
      walk(menu.items)
      return {
        label: menu.label,
        commands: COMMANDS.filter((c) => ids.has(c.id) && c.shortcut),
      }
    }).filter((group) => group.commands.length)
  }, [])

  return (
    <Modal open={open} onClose={onClose} title="Keyboard shortcuts" width="max-w-2xl">
      <div className="grid gap-5 sm:grid-cols-2">
        {groups.map((group) => (
          <div key={group.label}>
            <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-[var(--color-ink-dim)]">
              {group.label}
            </div>
            <div className="space-y-1">
              {group.commands.map((command) => (
                <div key={command.id} className="flex items-center justify-between gap-4 text-xs">
                  <span>{command.label}</span>
                  <kbd className="rounded border border-[var(--color-edge)] bg-[var(--color-surface)] px-1.5 py-0.5 font-mono text-[10px]">
                    {formatShortcut(command.shortcut)}
                  </kbd>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>

      <div className="mt-5 border-t border-[var(--color-edge)] pt-3 text-[11px] text-[var(--color-ink-dim)]">
        On the shot canvas: drag from an output port to an input port to chain steps. Delete removes the
        selected step or clip. Space-drag or middle-drag pans the canvas.
      </div>
    </Modal>
  )
}

function AboutDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: api.health,
    enabled: open,
  })

  return (
    <Modal open={open} onClose={onClose} title="About ComfyWebStudio">
      <div className="space-y-4">
        <div className="flex items-center gap-3">
          <span className="grid size-12 place-items-center rounded-xl bg-[var(--color-accent)] text-lg font-semibold text-white">
            CW
          </span>
          <div>
            <div className="text-base font-semibold">ComfyWebStudio</div>
            <div className="text-xs text-[var(--color-ink-dim)]">
              Version {health?.version ?? '…'} · node pack protocol {health?.protocol ?? '…'}
            </div>
          </div>
        </div>

        <p className="text-xs leading-relaxed text-[var(--color-ink-dim)]">
          Shot-based orchestration for ComfyUI. Build a shot from several workflows, chain each one's
          outputs into the next, preview every result, and cut the finished shots together on a timeline.
        </p>

        <div className="rounded-md border border-[var(--color-edge)] p-3 text-xs">
          <div className="mb-1.5 font-medium">Connected backends</div>
          {health?.backends?.length ? (
            health.backends.map((backend: any) => (
              <div key={backend.id} className="flex items-center gap-2 py-0.5">
                <Badge tone={backend.enabled ? 'ok' : 'muted'}>{backend.kind}</Badge>
                <span className="font-mono text-[11px]">{backend.base_url}</span>
                {backend.shared_filesystem && <Badge tone="info">shared fs</Badge>}
              </div>
            ))
          ) : (
            <span className="text-[var(--color-ink-dim)]">None configured.</span>
          )}
        </div>

        <div className="text-[11px] text-[var(--color-ink-dim)]">
          State directory: <span className="font-mono">{health?.root}</span>
        </div>
      </div>
    </Modal>
  )
}

// -- File -----------------------------------------------------------------------------------------------

function NewProjectDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')

  const create = useMutation({
    mutationFn: () => api.projects.create(name.trim() || 'Untitled Project', description.trim()),
    onSuccess: (project) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      onClose()
      setName('')
      setDescription('')
      navigate(`/p/${project.id}/shots`)
    },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  return (
    <Modal open={open} onClose={onClose} title="New project">
      <div className="space-y-3">
        <Field label="Name">
          <TextInput
            autoFocus
            value={name}
            placeholder="Untitled Project"
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && create.mutate()}
          />
        </Field>
        <Field label="Description" hint="optional">
          <TextArea value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <div className="flex justify-end gap-2 pt-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={create.isPending} onClick={() => create.mutate()}>
            {create.isPending ? <Spinner /> : null} Create
          </Button>
        </div>
      </div>
    </Modal>
  )
}

function ExportProjectDialog({
  open, onClose, project,
}: { open: boolean; onClose: () => void; project: Project | null }) {
  const [assets, setAssets] = useState(true)
  const [renders, setRenders] = useState(false)

  if (!project) return null

  return (
    <Modal open={open} onClose={onClose} title={`Export “${project.name}”`}>
      <div className="space-y-3">
        <Checkbox
          checked={assets}
          onChange={setAssets}
          label="Include generated media (previews will work after import)"
        />
        <Checkbox checked={renders} onChange={setRenders} label="Include rendered timeline output" />

        {!assets && (
          <Callout tone="warn">
            Without media, the project's structure and workflows are preserved but every preview will be
            missing until the shots are re-run.
          </Callout>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button onClick={onClose}>Cancel</Button>
          <a href={api.projects.exportUrl(project.id, { assets, renders })} download onClick={onClose}>
            <Button variant="primary">Export</Button>
          </a>
        </div>
      </div>
    </Modal>
  )
}

// -- Plugins --------------------------------------------------------------------------------------------

function PluginsDialog({
  open, onClose, project,
}: { open: boolean; onClose: () => void; project: Project | null }) {
  const toast = useToast()
  const queryClient = useQueryClient()

  const { data: plugins, isLoading } = useQuery({
    queryKey: ['plugins'],
    queryFn: api.plugins.list,
    enabled: open,
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['plugins'] })

  const act = async (action: () => Promise<unknown>, success: string) => {
    try {
      await action()
      toast.push('ok', success)
      refresh()
      queryClient.invalidateQueries({ queryKey: ['project', project?.id] })
    } catch (error) {
      toast.push('bad', error instanceof ApiError ? error.message : String(error))
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="Plugins" width="max-w-2xl">
      <p className="mb-3 text-xs leading-relaxed text-[var(--color-ink-dim)]">
        A plugin is a reusable bundle of workflows and shot structures. Applying one copies its content
        into the open project with fresh ids, so the plugin stays a template. Plugins contain content
        only, never executable code.
      </p>

      {isLoading ? (
        <div className="py-8 text-center text-xs text-[var(--color-ink-dim)]">
          <Spinner /> Loading…
        </div>
      ) : !plugins?.length ? (
        <div className="rounded-md border border-dashed border-[var(--color-edge)] py-8 text-center text-xs text-[var(--color-ink-dim)]">
          No plugins installed. Use <b>Plugins → Load Plugin…</b> to install a <code>.cwsplugin</code>{' '}
          file, or <b>Save Project as Plugin…</b> to make one.
        </div>
      ) : (
        <div className="space-y-2">
          {plugins.map((plugin) => (
            <div
              key={plugin.id}
              className={cx(
                'rounded-md border border-[var(--color-edge)] p-3',
                !plugin.enabled && 'opacity-50',
              )}
            >
              <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium">{plugin.name}</span>
                    <Badge tone="muted">v{plugin.version}</Badge>
                    {!plugin.enabled && <Badge tone="warn">disabled</Badge>}
                  </div>
                  {plugin.description && (
                    <div className="mt-0.5 text-xs text-[var(--color-ink-dim)]">{plugin.description}</div>
                  )}
                  <div className="mt-1 text-[11px] text-[var(--color-ink-dim)]">
                    {plugin.workflows.length} workflow(s) · {plugin.shot_templates.length} shot template(s)
                    {plugin.author && ` · by ${plugin.author}`}
                  </div>
                </div>
              </div>

              <div className="mt-2 flex flex-wrap gap-1">
                <Button
                  size="sm"
                  variant="primary"
                  disabled={!project || !plugin.enabled}
                  title={project ? 'Copy this plugin into the open project' : 'Open a project first'}
                  onClick={() =>
                    act(
                      () => api.plugins.apply(plugin.id, project!.id),
                      `Applied ${plugin.name} to ${project!.name}.`,
                    )
                  }
                >
                  Apply to project
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() =>
                    act(
                      () => api.plugins.setEnabled(plugin.id, !plugin.enabled),
                      plugin.enabled ? 'Disabled.' : 'Enabled.',
                    )
                  }
                >
                  {plugin.enabled ? 'Disable' : 'Enable'}
                </Button>
                <a href={api.plugins.downloadUrl(plugin.id)} download>
                  <Button size="sm" variant="ghost">Export</Button>
                </a>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    if (!confirm(`Uninstall “${plugin.name}”? Projects already using it are unaffected.`))
                      return
                    void act(() => api.plugins.uninstall(plugin.id), 'Uninstalled.')
                  }}
                >
                  Uninstall
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
    </Modal>
  )
}

function BuildPluginDialog({
  open, onClose, project,
}: { open: boolean; onClose: () => void; project: Project | null }) {
  const toast = useToast()
  const [name, setName] = useState('')
  const [author, setAuthor] = useState('')
  const [description, setDescription] = useState('')
  const [workflowIds, setWorkflowIds] = useState<string[]>([])
  const [shotIds, setShotIds] = useState<string[]>([])
  const [busy, setBusy] = useState(false)

  if (!project) return null

  const workflows = Object.values(project.workflows)
  const toggle = (list: string[], set: (v: string[]) => void, id: string) =>
    set(list.includes(id) ? list.filter((x) => x !== id) : [...list, id])

  const build = async () => {
    setBusy(true)
    try {
      const response = await fetch(api.plugins.buildUrl(project.id), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name.trim() || project.name,
          workflow_ids: workflowIds,
          shot_ids: shotIds.length ? shotIds : null,
          author,
          description,
        }),
      })
      if (!response.ok) {
        const payload = await response.json().catch(() => null)
        throw new ApiError(payload?.message ?? 'Could not build the plugin.', response.status)
      }
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `${(name.trim() || project.name).replace(/\s+/g, '-').toLowerCase()}.cwsplugin`
      anchor.click()
      URL.revokeObjectURL(url)
      toast.push('ok', 'Plugin built.')
      onClose()
    } catch (error) {
      toast.push('bad', error instanceof ApiError ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="Save as plugin" width="max-w-xl">
      <div className="space-y-3">
        <Field label="Plugin name">
          <TextInput value={name} placeholder={project.name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Author" hint="optional">
            <TextInput value={author} onChange={(e) => setAuthor(e.target.value)} />
          </Field>
          <Field label="Description" hint="optional">
            <TextInput value={description} onChange={(e) => setDescription(e.target.value)} />
          </Field>
        </div>

        <div>
          <div className="mb-1 text-xs font-medium text-[var(--color-ink-dim)]">Workflows</div>
          <div className="max-h-40 space-y-1 overflow-y-auto rounded-md border border-[var(--color-edge)] p-2">
            {workflows.map((workflow) => (
              <Checkbox
                key={workflow.id}
                checked={workflowIds.includes(workflow.id)}
                onChange={() => toggle(workflowIds, setWorkflowIds, workflow.id)}
                label={`${workflow.name} (${workflow.ports.length} ports)`}
              />
            ))}
            {!workflows.length && (
              <div className="text-xs text-[var(--color-ink-dim)]">This project has no workflows.</div>
            )}
          </div>
        </div>

        <div>
          <div className="mb-1 text-xs font-medium text-[var(--color-ink-dim)]">
            Shots <span className="opacity-60">— their workflows are included automatically</span>
          </div>
          <div className="max-h-32 space-y-1 overflow-y-auto rounded-md border border-[var(--color-edge)] p-2">
            {project.shots.map((shot) => (
              <Checkbox
                key={shot.id}
                checked={shotIds.includes(shot.id)}
                onChange={() => toggle(shotIds, setShotIds, shot.id)}
                label={`${shot.name} (${shot.steps.length} steps)`}
              />
            ))}
            {!project.shots.length && (
              <div className="text-xs text-[var(--color-ink-dim)]">This project has no shots.</div>
            )}
          </div>
        </div>

        <Callout tone="info">
          Generated media and run history are never included — a plugin is a template, not an archive. Use
          <b> File → Export Project</b> if you want the results too.
        </Callout>

        <div className="flex justify-end gap-2 pt-1">
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            disabled={busy || (!workflowIds.length && !shotIds.length)}
            onClick={build}
          >
            {busy ? <Spinner /> : null} Build plugin
          </Button>
        </div>
      </div>
    </Modal>
  )
}
