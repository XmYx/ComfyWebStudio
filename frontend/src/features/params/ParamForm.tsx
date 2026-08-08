/**
 * Editable parameters for one step.
 *
 * Widgets are chosen from a registry keyed by parameter kind, so supporting a new kind means adding one
 * entry rather than extending a switch in several places. Edits are debounced and PATCHed as a partial
 * override map — a value the user has not touched keeps falling back to the workflow's own default.
 */

import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { ParamSpec } from '@/api/types'
import { Badge, Button, Select, TextArea, TextInput, cx } from '@/components/ui'

const SAVE_DEBOUNCE_MS = 400

interface Props {
  params: ParamSpec[]
  overrides: Record<string, unknown>
  /** Port keys currently driven by a link — those values come from upstream, so they are read-only here. */
  linkedKeys: Set<string>
  onChange: (overrides: Record<string, unknown>) => void
  onReset: () => void
  onUnexpose?: (key: string) => void
}

export function ParamForm({ params, overrides, linkedKeys, onChange, onReset, onUnexpose }: Props) {
  const [draft, setDraft] = useState<Record<string, unknown>>(overrides)
  const timer = useRef<number>()

  // Adopt server state when the selection changes, but do not clobber what the user is typing.
  useEffect(() => setDraft(overrides), [overrides])

  const update = (key: string, value: unknown) => {
    const next = { ...draft, [key]: value }
    setDraft(next)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => onChange({ [key]: value }), SAVE_DEBOUNCE_MS)
  }

  const groups = useMemo(() => {
    const byGroup = new Map<string, ParamSpec[]>()
    for (const param of [...params].sort((a, b) => a.order - b.order || a.key.localeCompare(b.key))) {
      const group = param.group || 'Parameters'
      byGroup.set(group, [...(byGroup.get(group) ?? []), param])
    }
    return [...byGroup.entries()]
  }, [params])

  if (!params.length) {
    return (
      <div className="p-3 text-xs text-[var(--color-ink-dim)]">
        This workflow exposes no parameters. Add WebStudio input nodes in ComfyUI, or expose an existing
        node's widget with “Expose parameter”.
      </div>
    )
  }

  return (
    <div className="space-y-4 p-3">
      {groups.map(([group, groupParams]) => (
        <div key={group}>
          <div className="mb-2 text-[10px] font-semibold uppercase tracking-wide text-[var(--color-ink-dim)]">
            {group}
          </div>
          <div className="space-y-3">
            {groupParams.map((param) => (
              <ParamRow
                key={param.key}
                param={param}
                value={draft[param.key] ?? param.default}
                linked={linkedKeys.has(param.key)}
                onChange={(value) => update(param.key, value)}
                onUnexpose={onUnexpose}
              />
            ))}
          </div>
        </div>
      ))}

      <div className="border-t border-[var(--color-edge)] pt-3">
        <Button size="sm" variant="ghost" onClick={onReset}>Reset to workflow defaults</Button>
      </div>
    </div>
  )
}

function ParamRow({
  param, value, linked, onChange, onUnexpose,
}: {
  param: ParamSpec
  value: unknown
  linked: boolean
  onChange: (value: unknown) => void
  onUnexpose?: (key: string) => void
}) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-xs font-medium" title={param.tooltip}>
          {param.label || param.key}
          {param.is_seed && <Badge tone="muted">seed</Badge>}
          {param.source === 'raw_widget' && <Badge tone="info" title="Exposed from a node widget">raw</Badge>}
        </span>
        <div className="flex items-center gap-1">
          {linked && <Badge tone="ok" title="Driven by a link from another step">linked</Badge>}
          {param.source === 'raw_widget' && onUnexpose && (
            <button
              title="Stop exposing this parameter"
              className="text-[10px] text-[var(--color-ink-dim)] hover:text-[var(--color-bad)]"
              onClick={() => onUnexpose(param.key)}
            >
              ✕
            </button>
          )}
        </div>
      </div>

      <div className={cx(linked && 'pointer-events-none opacity-50')}>
        <Widget param={param} value={value} onChange={onChange} />
      </div>

      {linked && (
        <div className="mt-1 text-[10px] text-[var(--color-ink-dim)]">
          This value comes from the connected step at run time.
        </div>
      )}
    </div>
  )
}

// -- widget registry ------------------------------------------------------------------------------------

type WidgetProps = { param: ParamSpec; value: unknown; onChange: (value: unknown) => void }

const WIDGETS: Record<string, (props: WidgetProps) => ReactNode> = {
  string: ({ param, value, onChange }) =>
    param.multiline ? (
      <TextArea value={String(value ?? '')} onChange={(e) => onChange(e.target.value)} rows={3} />
    ) : (
      <TextInput value={String(value ?? '')} onChange={(e) => onChange(e.target.value)} />
    ),

  int: ({ param, value, onChange }) => (
    <NumberWidget param={param} value={value} onChange={onChange} integer />
  ),

  float: ({ param, value, onChange }) => (
    <NumberWidget param={param} value={value} onChange={onChange} />
  ),

  boolean: ({ value, onChange }) => {
    const on = Boolean(value)
    return (
    <button
      onClick={() => onChange(!on)}
      className={cx(
        'flex h-6 w-11 items-center rounded-full px-0.5 transition-colors',
        on ? 'bg-[var(--color-accent)]' : 'bg-[var(--color-edge)]',
      )}
    >
      <span
        className={cx(
          'size-5 rounded-full bg-white transition-transform',
          on && 'translate-x-5',
        )}
      />
    </button>
    )
  },

  choice: ({ param, value, onChange }) => (
    <Select value={String(value ?? '')} onChange={(e) => onChange(e.target.value)}>
      {(param.choices ?? []).map((choice) => (
        <option key={choice} value={choice}>{choice}</option>
      ))}
    </Select>
  ),
}

function Widget({ param, value, onChange }: WidgetProps) {
  const render = WIDGETS[param.kind] ?? WIDGETS.string
  return <>{render({ param, value, onChange })}</>
}

function NumberWidget({
  param, value, onChange, integer,
}: WidgetProps & { integer?: boolean }) {
  const numeric = Number(value ?? 0)
  // Only show a slider when the workflow declared a real, finite range — otherwise it would be a
  // meaningless control over an arbitrary span.
  const hasRange =
    param.min != null && param.max != null && param.max > param.min && param.max - param.min <= 1e6

  return (
    <div className="flex items-center gap-2">
      {hasRange && (
        <input
          type="range"
          className="flex-1"
          min={param.min!}
          max={param.max!}
          step={param.step ?? (integer ? 1 : 0.01)}
          value={Number.isFinite(numeric) ? numeric : 0}
          onChange={(e) => onChange(integer ? parseInt(e.target.value, 10) : parseFloat(e.target.value))}
        />
      )}
      <TextInput
        type="number"
        className={hasRange ? 'w-24' : 'w-full'}
        value={Number.isFinite(numeric) ? numeric : 0}
        step={param.step ?? (integer ? 1 : 0.01)}
        onChange={(e) => {
          const parsed = integer ? parseInt(e.target.value, 10) : parseFloat(e.target.value)
          onChange(Number.isFinite(parsed) ? parsed : 0)
        }}
      />
    </div>
  )
}
