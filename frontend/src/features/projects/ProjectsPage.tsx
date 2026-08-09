import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import { relativeTime } from '@/lib/format'
import {
  Button, Callout, Empty, Field, Modal, Panel, Spinner, TextArea, TextInput, useToast,
} from '@/components/ui'
import { ContextMenu, useContextMenu, type MenuItem } from '@/components/ContextMenu'
import { useCommandContext } from '@/features/menu/useCommandContext'

export function ProjectsPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()
  const fileInput = useRef<HTMLInputElement>(null)

  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const commandContext = useCommandContext()
  const contextMenu = useContextMenu()

  const { data: projects, isLoading, error } = useQuery({
    queryKey: ['projects'],
    queryFn: api.projects.list,
  })

  const { data: notices } = useQuery({
    queryKey: ['settings', 'notices'],
    queryFn: api.settings.notices,
  })

  const create = useMutation({
    mutationFn: () => api.projects.create(name.trim(), description.trim()),
    onSuccess: (project) => {
      setCreating(false)
      setName('')
      setDescription('')
      navigate(`/p/${project.id}/shots`)
    },
    onError: (err: ApiError) => toast.push('bad', err.message),
  })

  const importProject = useMutation({
    mutationFn: (file: File) => api.projects.import(file),
    onSuccess: (project) => {
      toast.push('ok', `Imported ${project.name}.`)
      navigate(`/p/${project.id}/shots`)
    },
    onError: (err: ApiError) => toast.push('bad', err.message),
  })

  const remove = useMutation({
    mutationFn: api.projects.remove,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['projects'] }),
    onError: (err: ApiError) => toast.push('bad', err.message),
  })

  return (
    <div className="mx-auto max-w-5xl p-6">
      <div className="mb-5 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Projects</h1>
          <p className="text-xs text-[var(--color-ink-dim)]">
            A project holds your workflows, shots and timeline.
          </p>
        </div>
        <div className="flex gap-2">
          <input
            ref={fileInput}
            type="file"
            accept=".cwsproj"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0]
              if (file) importProject.mutate(file)
              e.target.value = ''
            }}
          />
          <Button onClick={() => fileInput.current?.click()} disabled={importProject.isPending}>
            {importProject.isPending ? <Spinner /> : null} Import…
          </Button>
          <Button variant="primary" onClick={() => setCreating(true)}>New project</Button>
        </div>
      </div>

      {notices?.no_backends && (
        <div className="mb-4">
          <Callout tone="warn" title="No ComfyUI backend configured">
            Add one on the Settings page before you can run anything.
          </Callout>
        </div>
      )}

      {error && (
        <Callout tone="bad" title="Could not load projects">{(error as ApiError).message}</Callout>
      )}

      {isLoading ? (
        <Empty title="Loading…" />
      ) : !projects?.length ? (
        <Panel className="py-10">
          <Empty title="No projects yet">
            Create one to start building shots out of ComfyUI workflows.
          </Empty>
        </Panel>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {projects.map((project) => (
            <div
              key={project.id}
              onContextMenu={(event) =>
                contextMenu.open(event, [
                  { type: 'header', label: project.name },
                  { type: 'action', label: 'Open', onSelect: () => navigate(`/p/${project.id}/shots`) },
                  {
                    type: 'action',
                    label: 'Duplicate',
                    onSelect: async () => {
                      const copy = await api.projects.duplicate(project.id)
                      queryClient.invalidateQueries({ queryKey: ['projects'] })
                      toast.push('ok', `Duplicated as “${copy.name}”.`)
                    },
                  },
                  { type: 'separator' },
                  {
                    type: 'action',
                    label: 'Export…',
                    onSelect: () => {
                      const anchor = document.createElement('a')
                      anchor.href = api.projects.exportUrl(project.id)
                      anchor.download = ''
                      anchor.click()
                    },
                  },
                  { type: 'separator' },
                  {
                    type: 'action',
                    label: 'Delete',
                    danger: true,
                    onSelect: () => {
                      if (confirm(`Delete “${project.name}”? This removes its files from disk.`)) {
                        remove.mutate(project.id)
                      }
                    },
                  },
                ] satisfies MenuItem[])
              }
            >
            <Panel className="group flex flex-col p-4 transition-colors hover:border-[var(--color-accent)]/60">
              <button
                className="flex-1 text-left"
                onClick={() => navigate(`/p/${project.id}/shots`)}
              >
                <div className="truncate text-sm font-medium">{project.name}</div>
                {project.description && (
                  <div className="mt-1 line-clamp-2 text-xs text-[var(--color-ink-dim)]">
                    {project.description}
                  </div>
                )}
                <div className="mt-3 flex gap-3 text-[11px] text-[var(--color-ink-dim)]">
                  <span>{project.shot_count} shot{project.shot_count === 1 ? '' : 's'}</span>
                  <span>{project.workflow_count} workflow{project.workflow_count === 1 ? '' : 's'}</span>
                </div>
              </button>
              <div className="mt-3 flex items-center justify-between border-t border-[var(--color-edge)] pt-2">
                <span className="text-[11px] text-[var(--color-ink-dim)]">
                  {relativeTime(project.modified)}
                </span>
                <div className="flex gap-1 opacity-0 transition-opacity group-hover:opacity-100">
                  <a href={api.projects.exportUrl(project.id)} download>
                    <Button size="sm" variant="ghost" title="Export as .cwsproj">Export</Button>
                  </a>
                  <Button
                    size="sm"
                    variant="ghost"
                    title="Delete this project and all its files"
                    onClick={() => {
                      if (confirm(`Delete “${project.name}”? This removes its files from disk and cannot be undone.`)) {
                        remove.mutate(project.id)
                      }
                    }}
                  >
                    Delete
                  </Button>
                </div>
              </div>
            </Panel>
            </div>
          ))}
        </div>
      )}

      <Modal open={creating} onClose={() => setCreating(false)} title="New project">
        <div className="space-y-3">
          <Field label="Name">
            <TextInput
              autoFocus
              value={name}
              placeholder="Untitled Project"
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && name.trim() && create.mutate()}
            />
          </Field>
          <Field label="Description" hint="optional">
            <TextArea value={description} onChange={(e) => setDescription(e.target.value)} />
          </Field>
          <div className="flex justify-end gap-2 pt-2">
            <Button onClick={() => setCreating(false)}>Cancel</Button>
            <Button
              variant="primary"
              disabled={!name.trim() || create.isPending}
              onClick={() => create.mutate()}
            >
              {create.isPending ? <Spinner /> : null} Create
            </Button>
          </div>
        </div>
      </Modal>
      <ContextMenu state={contextMenu.menu} onClose={contextMenu.close} context={commandContext} />
    </div>
  )
}
