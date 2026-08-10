/**
 * A placed shot template: one node standing in for everything inside it.
 *
 * It looks like a step node on purpose — ports down the sides, controls in the middle — because from the
 * outside that is what it is. What differs is that its ports and controls come from the template rather
 * than from a workflow, and that it can fall behind: the template it points at may have moved on since it
 * was placed, which the header says plainly rather than leaving the user to find out at run time.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { Handle, NodeResizer, Position, useUpdateNodeInternals, type NodeProps } from '@xyflow/react'

import type { ParamSpec, PlacedTemplate, TemplateInstance, TemplatePort } from '@/api/types'
import { KIND_COLOR } from '@/lib/kinds'
import { ParamWidget } from '@/features/params/ParamForm'
import { Badge, ProgressBar, cx } from '@/components/ui'

const PARAM_SAVE_DEBOUNCE_MS = 400

export const MIN_TEMPLATE_NODE_WIDTH = 200
export const MIN_TEMPLATE_NODE_HEIGHT = 100
export const DEFAULT_TEMPLATE_NODE_WIDTH = 260

export interface TemplateNodeData extends Record<string, unknown> {
  instance: TemplateInstance
  /** The template's surface, or a marker that the library has lost it. */
  placed: PlacedTemplate | undefined
  /** Aggregate progress across the steps this instance expanded to. */
  live: { running: number; done: number; failed: number; progress: number } | undefined
  /** A preview of whatever the promoted output ports produced, keyed by promoted port. */
  previews: Record<string, string>
  linkedKeys: Set<string>
  /** Called with every pending control change at once — see `useControlPatch`. */
  onControlChange: (instanceId: string, values: Record<string, unknown>) => void
  onSync: (instanceId: string) => void
}

/**
 * Debounced, merged control writes.
 *
 * One node owns many controls, and a save is a load-modify-save of the whole project on the server. Two
 * controls edited at once therefore become two requests that each overwrite the other's work — with
 * unlucky timing both edits are lost. Coalescing them into a single PATCH removes the race rather than
 * narrowing it.
 */
function useControlPatch(
  instanceId: string, onControlChange: TemplateNodeData['onControlChange'],
) {
  const timer = useRef<number>()
  const pending = useRef<Record<string, unknown>>({})
  useEffect(() => () => window.clearTimeout(timer.current), [])

  return (key: string, value: unknown) => {
    pending.current = { ...pending.current, [key]: value }
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => {
      const values = pending.current
      pending.current = {}
      onControlChange(instanceId, values)
    }, PARAM_SAVE_DEBOUNCE_MS)
  }
}

export function TemplateNode({ id, data, selected }: NodeProps) {
  const { instance, placed, live, previews, linkedKeys, onControlChange, onSync } =
    data as TemplateNodeData
  const preview = Object.values(previews ?? {})[0] ?? null
  const ports = placed?.ports ?? []
  const controls = useMemo(() => placed?.controls ?? [], [placed])
  const inputs = ports.filter((p) => p.direction === 'in')
  const outputs = ports.filter((p) => p.direction === 'out')
  const title = instance.name || placed?.summary?.name || 'Template'
  // A nested shot is read live, so it has no revision to fall behind and never offers "update".
  const nested = Boolean(placed?.source_shot_id)

  const sized = instance.ui_size?.w > 0 && instance.ui_size?.h > 0
  const patchControl = useControlPatch(instance.id, onControlChange)

  // Same reason as the step node: React Flow only re-measures handles when a node changes size, and a
  // template gaining a port need not change this node's size at all.
  const portSignature = ports.map((p) => `${p.direction}:${p.key}`).join('|')
  const updateNodeInternals = useUpdateNodeInternals()
  useEffect(() => {
    updateNodeInternals(id)
  }, [id, portSignature, updateNodeInternals])

  if (placed?.missing) {
    return (
      <div className="w-56 rounded-lg border border-[var(--color-bad)] bg-[var(--color-panel)] p-2.5 shadow-lg">
        <div className="text-xs font-semibold">{title}</div>
        <div className="mt-1 text-[10px] leading-snug text-[var(--color-bad)]">
          {placed.source_shot_id
            ? 'The shot this stands for is no longer in this project. Delete this node.'
            : 'Its template is no longer in the library. Save a shot over it to restore it, or delete this node.'}
        </div>
      </div>
    )
  }

  return (
    <div
      className={cx(
        'flex flex-col overflow-hidden rounded-lg border-2 bg-[var(--color-panel)] shadow-lg transition-colors',
        selected ? 'border-[var(--color-accent)]' : 'border-[var(--color-edge)]',
        !instance.enabled && 'opacity-45',
      )}
      style={
        sized
          ? { width: instance.ui_size.w, height: instance.ui_size.h }
          : { width: DEFAULT_TEMPLATE_NODE_WIDTH }
      }
    >
      <NodeResizer
        isVisible={Boolean(selected)}
        minWidth={MIN_TEMPLATE_NODE_WIDTH}
        minHeight={MIN_TEMPLATE_NODE_HEIGHT}
        lineClassName="!border-[var(--color-accent)]"
        handleClassName="!bg-[var(--color-accent)] !border-[var(--color-panel)] !size-2"
      />

      <div className="flex shrink-0 items-center gap-1.5 border-b border-[var(--color-edge)] bg-[var(--color-panel-2)] px-2.5 py-1.5">
        <span
          className="text-[10px] text-[var(--color-ink-dim)]"
          title={nested ? 'Another shot, placed as one node' : 'A placed shot template'}
        >
          {nested ? '⧉' : '▣'}
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-semibold">{title}</div>
          <div className="truncate text-[10px] text-[var(--color-ink-dim)]">
            {placed?.summary
              ? `${nested ? 'shot' : 'template'} · ${placed.summary.step_count} step` +
                `${placed.summary.step_count === 1 ? '' : 's'}`
              : nested ? 'shot' : 'template'}
          </div>
        </div>
        {placed?.stale && (
          <button
            title="Its template has changed since this was placed. Update it."
            onClick={(e) => { e.stopPropagation(); onSync(instance.id) }}
            className="rounded bg-[var(--color-warn)]/15 px-1.5 py-0.5 text-[9px] text-[var(--color-warn)] hover:bg-[var(--color-warn)]/25"
          >
            update
          </button>
        )}
        {live && live.running > 0 && <Badge tone="info">{live.running} running</Badge>}
        {live && live.failed > 0 && <Badge tone="bad">{live.failed} failed</Badge>}
      </div>

      {live && live.running > 0 && (
        <div className="px-2.5 pt-1.5">
          <ProgressBar value={live.progress} />
        </div>
      )}

      {preview && (
        <div className="shrink-0 border-b border-[var(--color-edge)] bg-black/40">
          <img src={preview} alt="" className="h-24 w-full object-contain" />
        </div>
      )}

      {controls.length > 0 && (
        <div className="nodrag nowheel max-h-[45%] shrink-0 space-y-2 overflow-y-auto border-b border-[var(--color-edge)] px-2.5 py-2">
          {controls.map((control) => (
            <InstanceControl
              key={control.key}
              control={control}
              value={instance.param_overrides[control.key] ?? control.spec?.default}
              onChange={(value) => patchControl(control.key, value)}
            />
          ))}
        </div>
      )}

      <div className="grid shrink-0 max-h-[55%] grid-cols-2 gap-x-2 overflow-y-auto py-2 text-[10px]">
        <div className="space-y-1.5">
          {inputs.map((port) => (
            <TemplatePortRow key={port.key} port={port} side="left" linked={linkedKeys.has(port.key)} />
          ))}
        </div>
        <div className="space-y-1.5 text-right">
          {outputs.map((port) => (
            <TemplatePortRow
              key={port.key}
              port={port}
              side="right"
              // A produced port is drawn brighter, so it is obvious at a glance which ones have results.
              linked={Boolean(previews?.[port.key])}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

function InstanceControl({
  control, value, onChange,
}: {
  control: { key: string; label: string; spec: ParamSpec | null }
  value: unknown
  onChange: (value: unknown) => void
}) {
  // Local draft so the field stays responsive; the debounce lives on the node, which is where several
  // controls have to be coalesced into one save.
  const [draft, setDraft] = useState(value)
  useEffect(() => setDraft(value), [value])

  const update = (next: unknown) => {
    setDraft(next)
    onChange(next)
  }

  // A control with no spec is one whose inner parameter the template could not describe; showing the
  // label alone is more honest than guessing at a widget.
  if (!control.spec) {
    return (
      <div className="text-[9px] uppercase tracking-wide text-[var(--color-ink-dim)]">
        {control.label || control.key}
      </div>
    )
  }

  return (
    <div>
      <div
        className="mb-0.5 truncate text-[9px] uppercase tracking-wide text-[var(--color-ink-dim)]"
        title={control.label || control.key}
      >
        {control.label || control.key}
      </div>
      <ParamWidget param={control.spec} value={draft} onChange={update} />
    </div>
  )
}

function TemplatePortRow({
  port, side, linked,
}: { port: TemplatePort; side: 'left' | 'right'; linked: boolean }) {
  const color = KIND_COLOR[port.kind] ?? 'var(--kind-file)'
  return (
    <div className={cx('relative flex items-center gap-1 px-2.5', side === 'right' && 'justify-end')}>
      <Handle
        id={port.key}
        type={side === 'left' ? 'target' : 'source'}
        position={side === 'left' ? Position.Left : Position.Right}
        style={{ background: color, top: 'auto', transform: 'none' }}
        className="!relative !size-2.5"
      />
      <span
        className={cx('truncate', linked ? 'text-[var(--color-ink)]' : 'text-[var(--color-ink-dim)]')}
        title={`${port.label || port.key} (${port.kind}) · from ${port.inner_key}.${port.inner_port}`}
        style={{ order: side === 'left' ? 1 : -1 }}
      >
        {port.label || port.key}
      </span>
    </div>
  )
}
