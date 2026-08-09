/**
 * A value node on the shot canvas: a constant that feeds one or more step inputs.
 *
 * It is edited in place rather than in the inspector. The whole point of putting a value on the canvas is
 * that it is the value several steps share — having to select the node to see it would defeat that.
 *
 * Edits are debounced and PATCHed, the same contract the parameter form uses, so dragging a slider or
 * typing a prompt does not produce one request per keystroke.
 */

import { useEffect, useRef, useState } from 'react'
import { Handle, NodeResizer, Position, type NodeProps } from '@xyflow/react'

import { api } from '@/api/client'
import type { Asset, PortKind, ValueNode, ValueNodeKind } from '@/api/types'
import { KIND_COLOR, KIND_ICON } from '@/lib/kinds'
import { Select, TextArea, TextInput, cx } from '@/components/ui'
import { VALUE_PORT } from '@/api/types'

const SAVE_DEBOUNCE_MS = 400

export const MIN_VALUE_NODE_WIDTH = 150
export const MIN_VALUE_NODE_HEIGHT = 70
export const DEFAULT_VALUE_NODE_WIDTH = 200

/** Media kinds a media node can offer before an asset is chosen. */
const MEDIA_KINDS: PortKind[] = ['image', 'video', 'audio', 'mask', 'latent', 'file']

export const VALUE_NODE_LABELS: Record<ValueNodeKind, string> = {
  string: 'Text',
  int: 'Integer',
  float: 'Number',
  boolean: 'Boolean',
  media: 'Media',
}

export interface ValueNodeData extends Record<string, unknown> {
  node: ValueNode
  projectId: string
  /** Project assets, for the media picker. */
  assets: Asset[]
  outputKind: PortKind
  onChanged: () => void
}

export function ValueNodeCard({ data, selected }: NodeProps) {
  const { node, projectId, assets, outputKind, onChanged } = data as ValueNodeData
  const color = KIND_COLOR[outputKind] ?? 'var(--kind-file)'
  const sized = node.ui_size?.w > 0 && node.ui_size?.h > 0

  const patch = usePatch(projectId, node.id, onChanged)

  return (
    <div
      className={cx(
        'flex flex-col overflow-hidden rounded-lg border bg-[var(--color-panel)] shadow-lg transition-colors',
        selected ? 'border-[var(--color-accent)]' : 'border-[var(--color-edge)]',
      )}
      style={sized ? { width: node.ui_size.w, height: node.ui_size.h } : { width: DEFAULT_VALUE_NODE_WIDTH }}
    >
      <NodeResizer
        isVisible={Boolean(selected)}
        minWidth={MIN_VALUE_NODE_WIDTH}
        minHeight={MIN_VALUE_NODE_HEIGHT}
        lineClassName="!border-[var(--color-accent)]"
        handleClassName="!bg-[var(--color-accent)] !border-[var(--color-panel)] !size-2"
      />

      <div className="flex shrink-0 items-center gap-1.5 border-b border-[var(--color-edge)] px-2 py-1.5">
        <span className="text-[10px]" style={{ color }}>{KIND_ICON[outputKind] ?? '•'}</span>
        {/* The name is the label downstream steps are read against, so it is editable right here. */}
        <input
          value={node.name}
          placeholder={VALUE_NODE_LABELS[node.kind]}
          onChange={(e) => patch({ name: e.target.value })}
          className="nodrag min-w-0 flex-1 bg-transparent text-[11px] font-semibold outline-none placeholder:font-normal placeholder:text-[var(--color-ink-dim)]"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {node.kind === 'media' ? (
          <MediaBody
            node={node}
            projectId={projectId}
            assets={assets}
            // The empty option means "none", which is a clear rather than an unknown asset id.
            onPick={(assetId) => patch(assetId ? { asset_id: assetId } : { clear_asset: true })}
            onKind={(kind) => patch({ media_kind: kind })}
          />
        ) : (
          <ScalarBody node={node} onChange={(value) => patch({ value })} />
        )}
      </div>

      <div className="relative flex shrink-0 items-center justify-end gap-1 border-t border-[var(--color-edge)] px-2 py-1 text-[10px]">
        <span className="text-[var(--color-ink-dim)]">{outputKind}</span>
        <Handle
          id={VALUE_PORT}
          type="source"
          position={Position.Right}
          style={{ background: color, top: 'auto', transform: 'none' }}
          className="!relative !size-2.5"
        />
      </div>
    </div>
  )
}

/**
 * Debounced PATCH, so typing does not produce a request per keystroke.
 *
 * Pending fields accumulate rather than replace each other. One node owns several of them — a name, a
 * value, a media kind — and renaming a node just after typing into it must not throw the typing away.
 */
function usePatch(projectId: string, nodeId: string, onChanged: () => void) {
  type Patch = Partial<ValueNode> & { clear_value?: boolean; clear_asset?: boolean }
  const timer = useRef<number>()
  const pending = useRef<Patch>({})
  useEffect(() => () => window.clearTimeout(timer.current), [])

  return (patch: Patch) => {
    pending.current = { ...pending.current, ...patch }
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => {
      const body = pending.current
      pending.current = {}
      void api.nodes.update(projectId, nodeId, body).then(onChanged)
    }, SAVE_DEBOUNCE_MS)
  }
}

function ScalarBody({ node, onChange }: { node: ValueNode; onChange: (value: unknown) => void }) {
  // Local state so the field stays responsive while the debounced save is pending.
  const [draft, setDraft] = useState(node.value)
  useEffect(() => setDraft(node.value), [node.value])

  const set = (value: unknown) => {
    setDraft(value)
    onChange(value)
  }

  if (node.kind === 'boolean') {
    const on = Boolean(draft)
    return (
      <button
        onClick={() => set(!on)}
        className={cx(
          'nodrag flex h-6 w-11 items-center rounded-full px-0.5 transition-colors',
          on ? 'bg-[var(--color-accent)]' : 'bg-[var(--color-edge)]',
        )}
      >
        <span className={cx('size-5 rounded-full bg-white transition-transform', on && 'translate-x-5')} />
      </button>
    )
  }

  if (node.kind === 'int' || node.kind === 'float') {
    const integer = node.kind === 'int'
    return (
      <TextInput
        type="number"
        className="nodrag"
        step={integer ? 1 : 0.01}
        value={Number.isFinite(Number(draft)) ? Number(draft) : 0}
        onChange={(e) => {
          const parsed = integer ? parseInt(e.target.value, 10) : parseFloat(e.target.value)
          set(Number.isFinite(parsed) ? parsed : 0)
        }}
      />
    )
  }

  return (
    <TextArea
      rows={3}
      className="nodrag"
      placeholder="Text…"
      value={String(draft ?? '')}
      onChange={(e) => set(e.target.value)}
    />
  )
}

function MediaBody({
  node, projectId, assets, onPick, onKind,
}: {
  node: ValueNode
  projectId: string
  assets: Asset[]
  onPick: (assetId: string) => void
  onKind: (kind: PortKind) => void
}) {
  const asset = assets.find((a) => a.id === node.asset_id) ?? null

  return (
    <div className="space-y-1.5">
      {asset?.thumb ? (
        <img
          src={api.media.url(projectId, asset.thumb)}
          alt={asset.name}
          className="max-h-28 w-full rounded object-contain"
        />
      ) : (
        <div className="rounded border border-dashed border-[var(--color-edge)] px-2 py-3 text-center text-[10px] text-[var(--color-ink-dim)]">
          No media selected
        </div>
      )}

      <Select
        className="nodrag !py-0.5 !text-[10px]"
        value={node.asset_id ?? ''}
        onChange={(e) => onPick(e.target.value)}
      >
        <option value="">
          {assets.length ? 'Choose imported media…' : 'Import media in the timeline first'}
        </option>
        {assets.map((candidate) => (
          <option key={candidate.id} value={candidate.id}>
            {candidate.name} ({candidate.kind})
          </option>
        ))}
      </Select>

      {/* Only meaningful while empty: once there is an asset, its own kind is what the port carries. */}
      {!asset && (
        <Select
          className="nodrag !py-0.5 !text-[10px]"
          value={node.media_kind}
          onChange={(e) => onKind(e.target.value as PortKind)}
          title="What this node will offer, so it can be wired up before the media is chosen"
        >
          {MEDIA_KINDS.map((kind) => (
            <option key={kind} value={kind}>outputs {kind}</option>
          ))}
        </Select>
      )}
    </div>
  )
}
