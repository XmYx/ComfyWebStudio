import { useEffect, useMemo, useRef, useState } from 'react'
import { Handle, NodeResizer, Position, useUpdateNodeInternals, type NodeProps } from '@xyflow/react'
import type { ParamSpec, PortSpec, Step, WorkflowRef } from '@/api/types'
import { KIND_COLOR } from '@/lib/kinds'
import { ParamWidget } from '@/features/params/ParamForm'
import { Badge, ProgressBar, cx } from '@/components/ui'
import type { LiveStep } from '@/store/studio'

const PARAM_SAVE_DEBOUNCE_MS = 400

export interface StepNodeData extends Record<string, unknown> {
  step: Step
  workflow: WorkflowRef | undefined
  live: LiveStep | undefined
  thumbUrl: string | null
  selected: boolean
  /** Input port keys driven by a link, so a pinned parameter for one is shown but not editable. */
  linkedKeys: Set<string>
  onRun: (stepId: string) => void
  onToggle: (stepId: string) => void
  onParamChange: (stepId: string, key: string, value: unknown) => void
}

/** Below this a node cannot show its ports legibly. */
export const MIN_NODE_WIDTH = 180
export const MIN_NODE_HEIGHT = 90
export const DEFAULT_NODE_WIDTH = 240

const STATUS_TONE = {
  running: 'info', success: 'ok', cached: 'ok', error: 'bad',
  cancelled: 'warn', skipped: 'warn', pending: 'muted', queued: 'muted',
} as const

/**
 * One step on the shot canvas.
 *
 * Ports are the connection points — an input handle per input port on the left, an output handle per
 * output port on the right, colour-coded by kind so a mismatch is visible before you try to drag it.
 */
export function StepNode({ id, data, selected }: NodeProps) {
  const { step, workflow, live, thumbUrl, linkedKeys, onRun, onToggle, onParamChange } =
    data as StepNodeData
  const inputs = workflow?.ports.filter((p) => p.direction === 'in') ?? []
  const outputs = workflow?.ports.filter((p) => p.direction === 'out') ?? []
  const status = live?.status

  // Pinned parameters, in the order the user pinned them. A key whose parameter has since disappeared
  // from the workflow simply drops out rather than rendering an empty row.
  const pinned = useMemo(() => {
    const byKey = new Map((workflow?.params ?? []).map((p) => [p.key, p]))
    return step.exposed_params
      .map((key) => byKey.get(key))
      .filter((p): p is ParamSpec => p !== undefined)
  }, [workflow?.params, step.exposed_params])

  // A node that has never been resized sizes to its own content; once resized, the stored size wins and
  // the body scrolls rather than the node growing on its own.
  const sized = step.ui_size?.w > 0 && step.ui_size?.h > 0

  // React Flow measures a node's handles once and then only again when the node itself resizes. Ports
  // discovered by a re-sync change the handle set without changing the node's size — on a node the user
  // has resized, nothing changes size at all — so the new port would render but stay unconnectable, and
  // any edge to it would silently fail to draw. Telling React Flow explicitly is the documented fix.
  const portSignature = [...inputs, ...outputs].map((p) => `${p.direction}:${p.key}`).join('|')
  const updateNodeInternals = useUpdateNodeInternals()
  useEffect(() => {
    updateNodeInternals(id)
  }, [id, portSignature, updateNodeInternals])

  return (
    <div
      className={cx(
        'flex flex-col overflow-hidden rounded-lg border bg-[var(--color-panel)] shadow-lg transition-colors',
        data.selected ? 'border-[var(--color-accent)]' : 'border-[var(--color-edge)]',
        !step.enabled && 'opacity-45',
      )}
      style={
        sized
          ? { width: step.ui_size.w, height: step.ui_size.h }
          : { width: DEFAULT_NODE_WIDTH }
      }
    >
      <NodeResizer
        isVisible={Boolean(selected)}
        minWidth={MIN_NODE_WIDTH}
        minHeight={MIN_NODE_HEIGHT}
        lineClassName="!border-[var(--color-accent)]"
        handleClassName="!bg-[var(--color-accent)] !border-[var(--color-panel)] !size-2"
      />
      <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-edge)] px-2.5 py-2">
        <button
          title={step.enabled ? 'Disable this step' : 'Enable this step'}
          onClick={(e) => { e.stopPropagation(); onToggle(step.id) }}
          className={cx(
            'size-2.5 shrink-0 rounded-full transition-colors',
            step.enabled ? 'bg-[var(--color-ok)]' : 'bg-[var(--color-edge)]',
          )}
        />
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-semibold">{step.name}</div>
          <div className="truncate text-[10px] text-[var(--color-ink-dim)]">
            {workflow?.name ?? 'missing workflow'}
          </div>
        </div>
        {status && status !== 'pending' && (
          <Badge tone={STATUS_TONE[status] ?? 'muted'}>
            {status === 'cached' ? 'cached' : status}
          </Badge>
        )}
        <button
          title="Run this step (and anything it depends on)"
          onClick={(e) => { e.stopPropagation(); onRun(step.id) }}
          className="rounded px-1 text-[var(--color-ink-dim)] hover:bg-[var(--color-panel-2)] hover:text-[var(--color-accent)]"
        >
          ▶
        </button>
      </div>

      {status === 'running' && (
        <div className="px-2.5 pt-1.5">
          <ProgressBar value={live?.progress ?? 0} />
        </div>
      )}

      {thumbUrl && (
        <div className={cx('shrink-0 border-b border-[var(--color-edge)] bg-black/40', sized && 'min-h-0 flex-1')}>
          <img src={thumbUrl} alt="" className={cx('w-full object-contain', sized ? 'h-full' : 'h-24')} />
        </div>
      )}

      {live?.error && (
        <div className="border-b border-[var(--color-edge)] px-2.5 py-1.5 text-[10px] leading-snug text-[var(--color-bad)]">
          {live.error.slice(0, 160)}
        </div>
      )}

      {pinned.length > 0 && (
        // nodrag, or dragging a slider or selecting text would pan the node instead.
        <div className="nodrag nowheel max-h-[45%] shrink-0 space-y-2 overflow-y-auto border-t border-[var(--color-edge)] px-2.5 py-2">
          {pinned.map((param) => (
            <NodeParam
              key={param.key}
              param={param}
              value={step.param_overrides[param.key] ?? param.default}
              linked={linkedKeys.has(param.key)}
              onChange={(value) => onParamChange(step.id, param.key, value)}
            />
          ))}
        </div>
      )}

      {/*
        Ports keep their natural height and the preview absorbs whatever is left, so a port discovered by
        a re-sync is visible immediately rather than scrolled out of sight below a picture. Only once the
        list is taller than about half a resized node does it start scrolling — and on an unsized node the
        percentage resolves against an auto height, so nothing is capped and the node just grows.
      */}
      <div className="grid shrink-0 max-h-[55%] grid-cols-2 gap-x-2 overflow-y-auto py-2 text-[10px]">
        <div className="space-y-1.5">
          {inputs.map((port, index) => (
            <PortRow key={port.key} port={port} side="left" index={index} />
          ))}
        </div>
        <div className="space-y-1.5 text-right">
          {outputs.map((port, index) => (
            <PortRow key={port.key} port={port} side="right" index={index} />
          ))}
        </div>
      </div>

      {workflow?.missing_nodes?.length ? (
        <div className="border-t border-[var(--color-edge)] px-2.5 py-1 text-[10px] text-[var(--color-warn)]">
          {workflow.missing_nodes.length} node type(s) not installed
        </div>
      ) : null}
    </div>
  )
}

/**
 * One parameter the user pinned to this node.
 *
 * The same widget the inspector uses, so a slider stays a slider — only the label shrinks. Edits are held
 * locally and debounced, exactly as the inspector's form does it: typing a prompt should not be one
 * request per keystroke, and the field must not stutter while a save is in flight.
 */
function NodeParam({
  param, value, linked, onChange,
}: {
  param: ParamSpec
  value: unknown
  linked: boolean
  onChange: (value: unknown) => void
}) {
  const [draft, setDraft] = useState(value)
  const timer = useRef<number>()

  useEffect(() => setDraft(value), [value])
  useEffect(() => () => window.clearTimeout(timer.current), [])

  const update = (next: unknown) => {
    setDraft(next)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => onChange(next), PARAM_SAVE_DEBOUNCE_MS)
  }

  return (
    <div>
      <div
        className="mb-0.5 flex items-center gap-1 truncate text-[9px] uppercase tracking-wide text-[var(--color-ink-dim)]"
        title={param.tooltip || param.label || param.key}
      >
        <span className="truncate">{param.label || param.key}</span>
        {linked && <span className="text-[var(--color-ok)]">linked</span>}
      </div>
      <div className={cx(linked && 'pointer-events-none opacity-50')}>
        <ParamWidget param={param} value={draft} onChange={update} />
      </div>
    </div>
  )
}

function PortRow({ port, side, index }: { port: PortSpec; side: 'left' | 'right'; index: number }) {
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
        className="truncate text-[var(--color-ink-dim)]"
        title={`${port.label || port.key} (${port.kind})${port.optional ? ' · optional' : ''}`}
        style={{ order: side === 'left' ? 1 : -1 }}
      >
        {port.label || port.key}
      </span>
      <span className="sr-only">{index}</span>
    </div>
  )
}
