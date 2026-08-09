/**
 * The shared template library.
 *
 * Browsing on the left, the selected template's surface on the right. The surface is the interesting
 * half: it is what a placed node will look like, and it is editable here — hide a port you never wire,
 * rename a control whose auto-generated label is unhelpful. Both count as changing what instances can be
 * connected to, so both bump the revision and make placed instances read as out of date.
 */

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { Shot, TemplateSummary } from '@/api/types'
import { KIND_COLOR } from '@/lib/kinds'
import { relativeTime } from '@/lib/format'
import {
  Badge, Button, Modal, TextInput, cx, useToast,
} from '@/components/ui'

interface Props {
  open: boolean
  onClose: () => void
  projectId: string
  /** The shot a "Place" lands in, when there is one open. */
  shot: Shot | null
  onPlaced: () => void
}

export function TemplateLibrary({ open, onClose, projectId, shot, onPlaced }: Props) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [filter, setFilter] = useState('')

  const { data: templates, isLoading } = useQuery({
    queryKey: ['templates'],
    queryFn: api.templates.list,
    enabled: open,
  })

  const { data: template } = useQuery({
    queryKey: ['template', selectedId],
    queryFn: () => api.templates.get(selectedId!),
    enabled: open && Boolean(selectedId),
  })

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['templates'] })
    queryClient.invalidateQueries({ queryKey: ['template', selectedId] })
  }

  // Select the first template once the list arrives, so the panel is never pointlessly blank.
  useEffect(() => {
    if (open && templates?.length && !selectedId) setSelectedId(templates[0].id)
  }, [open, templates, selectedId])

  const place = useMutation({
    mutationFn: (templateId: string) =>
      api.instances.place(projectId, shot!.id, { template_id: templateId }),
    onSuccess: () => {
      toast.push('ok', 'Placed on the canvas.')
      onPlaced()
      onClose()
    },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  const matching = (templates ?? []).filter((entry) =>
    `${entry.name} ${entry.description}`.toLowerCase().includes(filter.toLowerCase()),
  )

  return (
    <Modal open={open} onClose={onClose} title="Template library" width="max-w-4xl">
      <p className="mb-3 text-xs text-[var(--color-ink-dim)]">
        Templates are shared by every project. Placing one puts it on the canvas as a single node and
        copies the workflows it needs into this project, so the project still runs on its own.
      </p>

      <div className="grid grid-cols-[260px_1fr] gap-3">
        <div className="min-w-0">
          <TextInput
            placeholder="Filter…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="mb-2"
          />
          <div className="max-h-96 space-y-1 overflow-y-auto">
            {isLoading && (
              <div className="py-6 text-center text-xs text-[var(--color-ink-dim)]">Loading…</div>
            )}
            {!isLoading && !matching.length && (
              <div className="py-6 text-center text-xs leading-relaxed text-[var(--color-ink-dim)]">
                {templates?.length
                  ? 'Nothing matches that filter.'
                  : 'No templates yet. Build a shot you like, then “Save Shot as Template”.'}
              </div>
            )}
            {matching.map((entry) => (
              <TemplateRow
                key={entry.id}
                entry={entry}
                selected={entry.id === selectedId}
                onSelect={() => setSelectedId(entry.id)}
              />
            ))}
          </div>
        </div>

        <div className="min-w-0">
          {!template ? (
            <div className="py-10 text-center text-xs text-[var(--color-ink-dim)]">
              Select a template to see what it exposes.
            </div>
          ) : (
            <div className="space-y-3">
              <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-semibold">{template.name}</div>
                  <div className="text-[10px] text-[var(--color-ink-dim)]">
                    revision {template.revision} · {template.steps.length} step
                    {template.steps.length === 1 ? '' : 's'}
                    {template.nodes.length > 0 && ` · ${template.nodes.length} value node`}
                    {template.source_project && ` · from ${template.source_project}`}
                  </div>
                </div>
                <Button
                  size="sm"
                  variant="primary"
                  disabled={!shot || place.isPending}
                  title={shot ? 'Place on the current shot' : 'Open a shot to place into'}
                  onClick={() => place.mutate(template.id)}
                >
                  Place
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  onClick={async () => {
                    if (!confirm(`Delete the template “${template.name}”?`)) return
                    try {
                      await api.templates.remove(template.id)
                      setSelectedId(null)
                      refresh()
                    } catch (error) {
                      toast.push('bad', (error as ApiError).message)
                    }
                  }}
                >
                  Delete
                </Button>
              </div>

              {template.description && (
                <div className="text-xs text-[var(--color-ink-dim)]">{template.description}</div>
              )}

              <Section title="Ports">
                {template.ports.length === 0 && <Empty>This template exposes no ports.</Empty>}
                {template.ports.map((port) => (
                  <SurfaceRow
                    key={`${port.direction}:${port.key}`}
                    shown={port.shown}
                    onToggle={async (shown) => {
                      await api.templates.setPort(template.id, port.key, { shown })
                      refresh()
                    }}
                    swatch={KIND_COLOR[port.kind]}
                    title={`${port.direction === 'in' ? '→' : '←'} ${port.label || port.key}`}
                    detail={`${port.kind} · ${port.inner_key}.${port.inner_port}`}
                  />
                ))}
              </Section>

              <Section title="Controls">
                {template.controls.length === 0 && <Empty>This template exposes no controls.</Empty>}
                {template.controls.map((control) => (
                  <SurfaceRow
                    key={control.key}
                    shown={control.shown}
                    onToggle={async (shown) => {
                      await api.templates.setControl(template.id, control.key, { shown })
                      refresh()
                    }}
                    title={control.label || control.key}
                    detail={`${control.spec?.kind ?? 'value'} · ${control.inner_key}.${control.inner_param}`}
                  />
                ))}
              </Section>
            </div>
          )}
        </div>
      </div>
    </Modal>
  )
}

function TemplateRow({
  entry, selected, onSelect,
}: { entry: TemplateSummary; selected: boolean; onSelect: () => void }) {
  return (
    <button
      onClick={onSelect}
      className={cx(
        'w-full rounded-md border px-2 py-1.5 text-left transition-colors',
        selected
          ? 'border-[var(--color-accent)] bg-[var(--color-accent)]/10'
          : 'border-[var(--color-edge)] hover:bg-[var(--color-panel-2)]',
      )}
    >
      <div className="truncate text-xs font-medium">{entry.name}</div>
      <div className="truncate text-[10px] text-[var(--color-ink-dim)]">
        {entry.step_count} step{entry.step_count === 1 ? '' : 's'} · {entry.input_count} in ·{' '}
        {entry.output_count} out · {entry.control_count} control
        {entry.control_count === 1 ? '' : 's'}
      </div>
      <div className="truncate text-[10px] text-[var(--color-ink-dim)]">
        {relativeTime(entry.modified)}
      </div>
    </button>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-[var(--color-ink-dim)]">
        {title}
      </div>
      <div className="max-h-44 space-y-0.5 overflow-y-auto">{children}</div>
    </div>
  )
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="py-2 text-xs text-[var(--color-ink-dim)]">{children}</div>
}

function SurfaceRow({
  shown, onToggle, title, detail, swatch,
}: {
  shown: boolean
  onToggle: (shown: boolean) => void
  title: string
  detail: string
  swatch?: string
}) {
  return (
    <label className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 hover:bg-[var(--color-panel-2)]">
      <input
        type="checkbox"
        checked={shown}
        onChange={(e) => onToggle(e.target.checked)}
        title="Show this on the placed node"
        className="size-3 shrink-0 accent-[var(--color-accent)]"
      />
      {swatch && <span className="size-2 shrink-0 rounded-full" style={{ background: swatch }} />}
      <span className="min-w-0 flex-1">
        <span className="block truncate text-xs">{title}</span>
        <span className="block truncate text-[10px] text-[var(--color-ink-dim)]">{detail}</span>
      </span>
      {!shown && <Badge tone="muted">hidden</Badge>}
    </label>
  )
}
