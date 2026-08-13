/**
 * The shape of a stage's answer, as a list of fields.
 *
 * The JSON Schema that constrains the model is generated from this on the server, which is the whole
 * reason it is a list rather than a text box: a schema you can mistype is a stage that stops working, and
 * the thing a user actually wants to say is "also give me a wardrobe note, and put it here".
 *
 * `writes` is a column of this table rather than a separate mapping, so renaming a field cannot leave a
 * destination pointing at nothing.
 */

import type { FieldType, OutputField } from '@/api/types'
import { Badge, Button, Select, TextInput, cx } from '@/components/ui'

const TYPES: Array<{ value: FieldType; label: string }> = [
  { value: 'string', label: 'a sentence' },
  { value: 'text', label: 'a paragraph' },
  { value: 'integer', label: 'a whole number' },
  { value: 'number', label: 'a number' },
  { value: 'boolean', label: 'yes or no' },
  { value: 'string_list', label: 'a list of words' },
  { value: 'object_list', label: 'a list of records' },
]

/** The prefixes that need a name of their own — `frame.fields.wardrobe` rather than a fixed field. */
const CUSTOM = ['frame.fields.', 'board.fields.']

const customPrefix = (writes: string) => CUSTOM.find((p) => writes.startsWith(p)) ?? null

/**
 * Where one answer goes.
 *
 * Most destinations are a fixed field and a plain pick. A *custom* one is two decisions — which side it
 * lives on, and what it is called — so choosing "somewhere of my own" reveals a name box rather than
 * making the user know that `frame.fields.wardrobe` is the spelling.
 */
function Destination({
  writes,
  writable,
  onChange,
}: {
  writes: string
  writable: string[]
  onChange: (writes: string) => void
}) {
  const prefix = customPrefix(writes)
  const options = writable.filter((t) => !t.endsWith('.*'))
  const customOptions = writable.filter((t) => t.endsWith('.*'))

  return (
    <div className="mt-1 space-y-1">
      <div className="flex items-center gap-1.5">
        <span className="w-12 shrink-0 text-[10px] text-[var(--color-ink-dim)]">goes to</span>
        <Select
          value={prefix ? `${prefix}*` : writes}
          className="h-6 flex-1 text-[11px]"
          onChange={(e) => {
            const chosen = e.target.value
            onChange(chosen.endsWith('.*') ? chosen.slice(0, -1) : chosen)
          }}
        >
          <option value="">nowhere — just show it</option>
          {options.map((target) => (
            <option key={target} value={target}>{target}</option>
          ))}
          {customOptions.map((target) => (
            <option key={target} value={target}>
              {target.startsWith('frame') ? 'somewhere of my own, on the frame' : 'somewhere of my own, on the board'}
            </option>
          ))}
          {/* A destination saved earlier that this stage's scope no longer offers stays selectable, so
              opening the editor cannot silently retarget it. */}
          {writes && !prefix && !options.includes(writes) && (
            <option value={writes}>{writes}</option>
          )}
        </Select>
      </div>

      {prefix && (
        <div className="flex items-center gap-1.5">
          <span className="w-12 shrink-0 text-[10px] text-[var(--color-ink-dim)]">called</span>
          <TextInput
            value={writes.slice(prefix.length)}
            placeholder="wardrobe"
            className="h-6 flex-1 font-mono text-[11px]"
            onChange={(e) => onChange(prefix + e.target.value)}
          />
          <Badge tone="info">{`{${prefix}${writes.slice(prefix.length) || '…'}}`}</Badge>
        </div>
      )}
    </div>
  )
}

export function OutputFields({
  fields,
  writable,
  onChange,
  nested = false,
}: {
  fields: OutputField[]
  writable: string[]
  onChange: (fields: OutputField[]) => void
  nested?: boolean
}) {
  const patch = (index: number, body: Partial<OutputField>) =>
    onChange(fields.map((f, i) => (i === index ? { ...f, ...body } : f)))

  const add = () =>
    onChange([
      ...fields,
      { key: '', type: 'string', description: '', required: true, fields: [], writes: '' },
    ])

  return (
    <div className="space-y-1.5">
      {fields.map((field, index) => (
        <div
          key={index}
          className={cx(
            'rounded border border-[var(--color-edge)] p-1.5',
            nested && 'bg-[var(--color-surface)]',
          )}
        >
          <div className="flex items-center gap-1.5">
            <TextInput
              value={field.key}
              placeholder="name"
              className="h-6 w-32 shrink-0 font-mono text-[11px]"
              onChange={(e) => patch(index, { key: e.target.value })}
            />
            <Select
              value={field.type}
              className="h-6 w-32 shrink-0 text-[11px]"
              onChange={(e) => patch(index, { type: e.target.value as FieldType })}
            >
              {TYPES.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </Select>
            <label className="flex shrink-0 items-center gap-1 text-[10px] text-[var(--color-ink-dim)]">
              <input
                type="checkbox"
                checked={field.required}
                onChange={(e) => patch(index, { required: e.target.checked })}
              />
              must answer
            </label>
            <div className="flex-1" />
            <Button
              size="sm"
              variant="ghost"
              title="Remove this field"
              onClick={() => onChange(fields.filter((_, i) => i !== index))}
            >
              ✕
            </Button>
          </div>

          <TextInput
            value={field.description}
            placeholder="what to ask for — the model reads this"
            className="mt-1 h-6 w-full text-[11px]"
            onChange={(e) => patch(index, { description: e.target.value })}
          />

          {field.type === 'object_list' ? (
            <div className="mt-1.5 border-l border-[var(--color-edge)] pl-2">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-[var(--color-ink-dim)]">
                each record holds
              </div>
              <OutputFields
                fields={field.fields}
                writable={writable}
                nested
                onChange={(inner) => patch(index, { fields: inner })}
              />
            </div>
          ) : null}

          {!nested && (
            <Destination
              writes={field.writes}
              writable={writable}
              onChange={(writes) => patch(index, { writes })}
            />
          )}
        </div>
      ))}

      <Button size="sm" variant="ghost" onClick={add}>+ Field</Button>
    </div>
  )
}
