import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import { formatBytes } from '@/lib/format'
import { Badge, Button, Callout, Modal, Spinner, TextInput, useToast } from '@/components/ui'

/**
 * Picks a workflow out of the ones already saved in ComfyUI.
 *
 * ComfyUI keeps them under `user/<user>/workflows` and serves them over `/userdata`, which behaves the
 * same for a local install and a cloud instance — so this works either way.
 */
export function ComfyWorkflowBrowser({
  open, onClose, projectId,
}: { open: boolean; onClose: () => void; projectId: string }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [filter, setFilter] = useState('')
  const [importing, setImporting] = useState<string | null>(null)

  const { data, isLoading, refetch, isRefetching } = useQuery({
    queryKey: ['comfy-workflows'],
    queryFn: () => api.workflows.fromComfy(),
    enabled: open,
  })

  const doImport = useMutation({
    mutationFn: ({ path, name }: { path: string; name: string }) =>
      api.workflows.importFromComfy(projectId, path, name),
    onMutate: ({ path }) => setImporting(path),
    onSettled: () => setImporting(null),
    onSuccess: (workflow) => {
      queryClient.invalidateQueries({ queryKey: ['project', projectId] })
      const ports = workflow.ports.length
      toast.push(
        ports ? 'ok' : 'info',
        ports
          ? `Imported ${workflow.name} — ${ports} port(s) found.`
          : `Imported ${workflow.name}, but it has no WebStudio ports yet. ` +
            `Open it in ComfyUI and add WS input/output nodes.`,
      )
      if (workflow.warnings.length) toast.push('info', workflow.warnings[0])
    },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  const workflows = (data?.workflows ?? []).filter((w) =>
    w.name.toLowerCase().includes(filter.toLowerCase()),
  )

  return (
    <Modal open={open} onClose={onClose} title="Import from ComfyUI" width="max-w-2xl">
      <p className="mb-3 text-xs text-[var(--color-ink-dim)]">
        These are the workflows saved in your ComfyUI instance
        {data?.backend ? ` (${data.backend})` : ''}. Importing copies one into this project; the original
        is left untouched.
      </p>

      {data && !data.reachable ? (
        <Callout tone="bad" title="ComfyUI is not reachable">
          {data.error} — check the backend on the Settings page.
        </Callout>
      ) : isLoading ? (
        <div className="py-8 text-center text-xs text-[var(--color-ink-dim)]">
          <Spinner /> Loading workflows…
        </div>
      ) : (
        <>
          <div className="mb-3 flex gap-2">
            <TextInput
              autoFocus
              placeholder="Filter by name…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
            <Button onClick={() => refetch()} disabled={isRefetching} title="Re-read the list from ComfyUI">
              {isRefetching ? <Spinner /> : '↻'} Refresh
            </Button>
          </div>

          <div className="max-h-96 space-y-1 overflow-y-auto">
            {workflows.map((workflow) => (
              <div
                key={workflow.path}
                className="flex items-center gap-2 rounded-md border border-[var(--color-edge)] px-2 py-1.5"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs font-medium">{workflow.name}</div>
                  <div className="truncate text-[10px] text-[var(--color-ink-dim)]">
                    {workflow.path}
                    {workflow.size ? ` · ${formatBytes(workflow.size)}` : ''}
                    {workflow.modified
                      ? ` · ${new Date(workflow.modified).toLocaleDateString()}`
                      : ''}
                  </div>
                </div>
                <Button
                  size="sm"
                  disabled={importing !== null}
                  onClick={() => doImport.mutate({ path: workflow.path, name: workflow.name })}
                >
                  {importing === workflow.path ? <Spinner /> : null} Import
                </Button>
              </div>
            ))}

            {!workflows.length && (
              <div className="py-8 text-center text-xs text-[var(--color-ink-dim)]">
                {filter
                  ? 'Nothing matches that filter.'
                  : 'ComfyUI has no saved workflows yet. Save one there first.'}
              </div>
            )}
          </div>

          <div className="mt-3 border-t border-[var(--color-edge)] pt-3">
            <Badge tone="muted">note</Badge>{' '}
            <span className="text-[11px] text-[var(--color-ink-dim)]">
              These are LiteGraph documents, so ComfyWebStudio converts them to a runnable prompt on
              import. If a workflow uses subgraphs, open it in ComfyUI and use “Save to ComfyWebStudio”
              instead — that lets ComfyUI do the conversion exactly.
            </span>
          </div>
        </>
      )}
    </Modal>
  )
}
