import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useStudio as useStudioStore } from '@/store/studio'

import { api, ApiError } from '@/api/client'
import type { Project, Shot, WorkflowRef } from '@/api/types'
import { KIND_COLOR } from '@/lib/kinds'
import { relativeTime } from '@/lib/format'
import { Badge, Button, Panel, PanelHeader, Spinner, useToast } from '@/components/ui'
import { ComfyWorkflowBrowser } from './ComfyWorkflowBrowser'
import { useLayout } from '@/store/layout'
import { ContextMenu, useContextMenu, type MenuItem } from '@/components/ContextMenu'
import { useCommandContext } from '@/features/menu/useCommandContext'

interface Props {
  project: Project
  shot: Shot | null
  onChanged: () => void
}

/** The project's workflow library: import, inspect, open in ComfyUI, add as a step. */
export function WorkflowLibrary({ project, shot, onChanged }: Props) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const fileInput = useRef<HTMLInputElement>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  // The File menu opens this dialog too, so its visibility lives in the layout store rather than here.
  const dialog = useLayout((s) => s.dialog)
  const openDialog = useLayout((s) => s.openDialog)
  const closeDialog = useLayout((s) => s.closeDialog)
  const browsing = dialog === 'comfyBrowser'
  const commandContext = useCommandContext()
  const contextMenu = useContextMenu()

  const workflowMenu = (workflow: WorkflowRef): MenuItem[] => [
    { type: 'header', label: workflow.name },
    { type: 'action', label: 'Add as a step', disabled: !shot, onSelect: () => void addStep(workflow) },
    { type: 'separator' },
    { type: 'action', label: 'Open in ComfyUI', onSelect: () => void openInComfy(workflow) },
    {
      type: 'action',
      label: 'Re-scan for ports',
      onSelect: async () => {
        try {
          await api.workflows.rediscover(project.id, workflow.id)
          queryClient.invalidateQueries({ queryKey: ['project', project.id] })
          toast.push('ok', 'Re-scanned.')
        } catch (error) {
          toast.push('bad', (error as ApiError).message)
        }
      },
    },
    {
      type: 'action',
      label: 'Show history…',
      onSelect: () => {
        useStudioStore.getState().setHistoryTarget({
          scope: 'workflow', id: workflow.id, name: workflow.name,
        })
        openDialog('history')
      },
    },
    { type: 'separator' },
    {
      type: 'action',
      label: 'Remove from project',
      danger: true,
      onSelect: async () => {
        if (!confirm(`Remove “${workflow.name}” from this project?`)) return
        try {
          await api.workflows.remove(project.id, workflow.id)
          queryClient.invalidateQueries({ queryKey: ['project', project.id] })
        } catch (error) {
          toast.push('bad', (error as ApiError).message)
        }
      },
    },
  ]

  const upload = useMutation({
    mutationFn: (file: File) => api.workflows.upload(project.id, file),
    onSuccess: (workflow) => {
      toast.push(
        workflow.warnings.length ? 'info' : 'ok',
        workflow.warnings.length
          ? `${workflow.name} imported with notes — check the workflow card.`
          : `${workflow.name} imported: ${workflow.ports.length} port(s).`,
      )
      queryClient.invalidateQueries({ queryKey: ['project', project.id] })
      onChanged()
    },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  const workflows = Object.values(project.workflows)

  const addStep = async (workflow: WorkflowRef) => {
    if (!shot) {
      toast.push('bad', 'Select or create a shot first.')
      return
    }
    try {
      await api.steps.create(project.id, shot.id, workflow.id)
      onChanged()
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    }
  }

  const openInComfy = async (workflow: WorkflowRef) => {
    setBusyId(workflow.id)
    try {
      const result = await api.workflows.openInComfy(project.id, workflow.id)
      if (result.hint) toast.push('bad', result.hint)
      window.open(result.url, '_blank', 'noopener')
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    } finally {
      setBusyId(null)
    }
  }

  return (
    <Panel className="flex min-h-0 flex-col">
      <PanelHeader
        actions={
          <>
            <input
              ref={fileInput}
              type="file"
              accept=".json"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0]
                if (file) upload.mutate(file)
                e.target.value = ''
              }}
            />
            <Button
              size="sm"
              onClick={() => openDialog('comfyBrowser')}
              title="Browse the workflows already saved in ComfyUI"
            >
              From ComfyUI
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => fileInput.current?.click()}
              disabled={upload.isPending}
              title="Import a workflow JSON file"
            >
              {upload.isPending ? <Spinner /> : '+'} File
            </Button>
          </>
        }
      >
        Workflows
      </PanelHeader>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {!workflows.length ? (
          <div className="p-3 text-xs leading-relaxed text-[var(--color-ink-dim)]">
            Press <b>From ComfyUI</b> to pick one of the workflows you already have saved there, or
            <b> File</b> to import a JSON you exported.
          </div>
        ) : (
          <div className="space-y-2">
            {workflows.map((workflow) => (
              <div
                key={workflow.id}
                onContextMenu={(event) => contextMenu.open(event, workflowMenu(workflow))}
                className="rounded-md border border-[var(--color-edge)] bg-[var(--color-surface)] p-2"
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate text-xs font-medium">{workflow.name}</div>
                    <div className="text-[10px] text-[var(--color-ink-dim)]">
                      synced {relativeTime(workflow.last_synced)}
                    </div>
                  </div>
                  <Button size="sm" variant="ghost" onClick={() => addStep(workflow)} title="Add as a step">
                    + Step
                  </Button>
                </div>

                <div className="mt-1.5 flex flex-wrap gap-1">
                  {workflow.ports.map((port) => (
                    <span
                      key={`${port.direction}:${port.key}`}
                      title={`${port.direction === 'in' ? 'input' : 'output'} · ${port.kind}`}
                      className="rounded px-1 py-0.5 text-[9px]"
                      style={{
                        background: `${KIND_COLOR[port.kind]}22`,
                        color: KIND_COLOR[port.kind],
                      }}
                    >
                      {port.direction === 'in' ? '→' : '←'} {port.key}
                    </span>
                  ))}
                  {!workflow.ports.length && <Badge tone="warn">no ports</Badge>}
                </div>

                {workflow.warnings.length > 0 && (
                  <div className="mt-1.5 space-y-0.5">
                    {workflow.warnings.slice(0, 2).map((warning, i) => (
                      <div key={i} className="text-[10px] leading-snug text-[var(--color-warn)]">
                        {warning}
                      </div>
                    ))}
                  </div>
                )}

                <div className="mt-1.5 flex gap-1">
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => openInComfy(workflow)}
                    disabled={busyId === workflow.id}
                    title="Open this workflow in ComfyUI; saving there syncs it back here"
                  >
                    {busyId === workflow.id ? <Spinner /> : null} Open in ComfyUI
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    title="Re-scan the stored graph for ports and parameters"
                    onClick={async () => {
                      try {
                        await api.workflows.rediscover(project.id, workflow.id)
                        queryClient.invalidateQueries({ queryKey: ['project', project.id] })
                        toast.push('ok', 'Re-scanned.')
                      } catch (error) {
                        toast.push('bad', (error as ApiError).message)
                      }
                    }}
                  >
                    Re-scan
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={async () => {
                      if (!confirm(`Remove “${workflow.name}” from this project?`)) return
                      try {
                        await api.workflows.remove(project.id, workflow.id)
                        queryClient.invalidateQueries({ queryKey: ['project', project.id] })
                      } catch (error) {
                        toast.push('bad', (error as ApiError).message)
                      }
                    }}
                  >
                    Remove
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <ComfyWorkflowBrowser
        open={browsing}
        onClose={closeDialog}
        projectId={project.id}
      />
      <ContextMenu state={contextMenu.menu} onClose={contextMenu.close} context={commandContext} />
    </Panel>
  )
}
