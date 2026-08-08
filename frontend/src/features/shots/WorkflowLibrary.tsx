import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { Project, Shot, WorkflowRef } from '@/api/types'
import { KIND_COLOR } from '@/lib/kinds'
import { relativeTime } from '@/lib/format'
import { Badge, Button, Panel, PanelHeader, Spinner, useToast } from '@/components/ui'

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
            <Button size="sm" onClick={() => fileInput.current?.click()} disabled={upload.isPending}>
              {upload.isPending ? <Spinner /> : '+'} Import
            </Button>
          </>
        }
      >
        Workflows
      </PanelHeader>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {!workflows.length ? (
          <div className="p-3 text-xs leading-relaxed text-[var(--color-ink-dim)]">
            Import a ComfyUI workflow JSON to get started. Use <b>Export (API)</b> in ComfyUI for the most
            reliable import, or drop in a normal workflow and open it in ComfyUI to sync it back exactly.
          </div>
        ) : (
          <div className="space-y-2">
            {workflows.map((workflow) => (
              <div
                key={workflow.id}
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
    </Panel>
  )
}
