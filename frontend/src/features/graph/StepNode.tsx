import { Handle, NodeResizer, Position, type NodeProps } from '@xyflow/react'
import type { PortSpec, Step, WorkflowRef } from '@/api/types'
import { KIND_COLOR } from '@/lib/kinds'
import { Badge, ProgressBar, cx } from '@/components/ui'
import type { LiveStep } from '@/store/studio'

export interface StepNodeData extends Record<string, unknown> {
  step: Step
  workflow: WorkflowRef | undefined
  live: LiveStep | undefined
  thumbUrl: string | null
  selected: boolean
  onRun: (stepId: string) => void
  onToggle: (stepId: string) => void
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
export function StepNode({ data, selected }: NodeProps) {
  const { step, workflow, live, thumbUrl, onRun, onToggle } = data as StepNodeData
  const inputs = workflow?.ports.filter((p) => p.direction === 'in') ?? []
  const outputs = workflow?.ports.filter((p) => p.direction === 'out') ?? []
  const status = live?.status

  // A node that has never been resized sizes to its own content; once resized, the stored size wins and
  // the body scrolls rather than the node growing on its own.
  const sized = step.ui_size?.w > 0 && step.ui_size?.h > 0

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

      <div className="grid min-h-0 flex-1 grid-cols-2 gap-x-2 overflow-y-auto py-2 text-[10px]">
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
