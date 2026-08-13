/**
 * One step of the flow, opened up.
 *
 * The prompts are the point. They are plain text with `{tokens}` in them, and the palette underneath is
 * generated from the board itself, so what a token is *worth* is visible next to what it is called — the
 * question when writing a prompt is rarely "does board.premise exist" and usually "what is in it".
 *
 * A token that will not resolve is underlined as it is typed, because the alternative is finding out from
 * a frame that came back wrong.
 */

import { useMemo, useState } from 'react'

import type { Stage, StageView } from '@/api/types'
import { Badge, Button, Callout, Field, Select, TextArea, TextInput } from '@/components/ui'

import { OutputFields } from './OutputFields'

const TOKEN = /\{([a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)*)\}/g

/** Which tokens in a template have no value — the same rule the server renders by. */
export function unknownTokens(template: string, tokens: Record<string, string>): string[] {
  const found = [...template.matchAll(TOKEN)].map((m) => m[1])
  return [...new Set(found)].filter((name) => !(name in tokens))
}

function TemplateBox({
  label,
  hint,
  value,
  tokens,
  rows,
  onChange,
}: {
  label: string
  hint?: string
  value: string
  tokens: Record<string, string>
  rows: number
  onChange: (value: string) => void
}) {
  const unknown = unknownTokens(value, tokens)
  return (
    <Field label={label} hint={hint}>
      <TextArea
        rows={rows}
        value={value}
        className="font-mono text-[11px] leading-snug"
        onChange={(e) => onChange(e.target.value)}
      />
      {unknown.length > 0 && (
        <div className="mt-1 text-[10px] text-[var(--color-warn)]">
          No such token: {unknown.map((t) => `{${t}}`).join(', ')}. It will be sent as written.
        </div>
      )}
    </Field>
  )
}

function TokenPalette({ tokens }: { tokens: Record<string, string> }) {
  const [open, setOpen] = useState(false)
  const names = useMemo(() => Object.keys(tokens).sort(), [tokens])

  return (
    <div className="rounded border border-[var(--color-edge)]">
      <button
        className="flex w-full items-center gap-2 px-2 py-1 text-left text-[10px] font-semibold uppercase tracking-wide text-[var(--color-ink-dim)]"
        onClick={() => setOpen(!open)}
      >
        <span>{open ? '▾' : '▸'}</span> Tokens you can use ({names.length})
      </button>
      {open && (
        <div className="max-h-48 overflow-y-auto border-t border-[var(--color-edge)] p-1">
          {names.map((name) => (
            <div key={name} className="flex gap-2 px-1 py-0.5 text-[10px]">
              <code className="w-40 shrink-0 text-[var(--color-accent)]">{`{${name}}`}</code>
              <span className="min-w-0 flex-1 truncate text-[var(--color-ink-dim)]">
                {tokens[name] || <em>empty</em>}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export function StageEditor({
  stage,
  tokens,
  saving,
  onSave,
  onReset,
  onClose,
}: {
  stage: StageView
  tokens: Record<string, string>
  saving?: boolean
  onSave: (stage: Stage) => void
  onReset: () => void
  onClose: () => void
}) {
  const [draft, setDraft] = useState<StageView>(stage)
  const patch = (body: Partial<Stage>) => setDraft({ ...draft, ...body })
  const dirty = JSON.stringify(draft) !== JSON.stringify(stage)

  return (
    <div className="space-y-2 p-2">
      <div className="flex items-center gap-2">
        <Button size="sm" variant="ghost" onClick={onClose}>← Back</Button>
        <span className="min-w-0 flex-1 truncate text-xs font-semibold">
          {draft.name || draft.id}
        </span>
        {draft.edited && <Badge tone="info">edited</Badge>}
        {draft.stale && <Badge tone="warn">default moved on</Badge>}
      </div>

      {draft.stale && (
        <Callout tone="warn">
          The built-in version of this step has changed since you edited it. Reset it to take the new
          one, or leave it — nothing will overwrite your wording.
        </Callout>
      )}

      <div className="grid grid-cols-2 gap-2">
        <Field label="Name">
          <TextInput
            value={draft.name}
            className="h-7 text-[11px]"
            onChange={(e) => patch({ name: e.target.value })}
          />
        </Field>
        <Field label="Runs" hint={draft.scope === 'frame' ? 'once per frame' : 'once for the board'}>
          <Select
            value={draft.scope}
            className="h-7 text-[11px]"
            onChange={(e) => patch({ scope: e.target.value as Stage['scope'] })}
          >
            <option value="board">for the whole board</option>
            <option value="frame">for each frame</option>
          </Select>
        </Field>
      </div>

      <Field label="What this step is for">
        <TextInput
          value={draft.description}
          className="h-7 text-[11px]"
          onChange={(e) => patch({ description: e.target.value })}
        />
      </Field>

      {draft.kind === 'llm' ? (
        <>
          <div className="grid grid-cols-3 gap-2">
            <Field label="Model" hint="which of the two">
              <Select
                value={draft.model.role}
                className="h-7 text-[11px]"
                onChange={(e) =>
                  patch({
                    model: { ...draft.model, role: e.target.value as 'write' | 'vision' },
                  })
                }
              >
                <option value="write">the one that writes</option>
                <option value="vision">the one that sees</option>
              </Select>
            </Field>
            <Field label="Temperature" hint="blank follows Settings">
              <TextInput
                type="number"
                step="0.05"
                min="0"
                max="2"
                value={draft.model.temperature ?? ''}
                className="h-7 text-[11px]"
                onChange={(e) =>
                  patch({
                    model: {
                      ...draft.model,
                      temperature: e.target.value === '' ? null : Number(e.target.value),
                    },
                  })
                }
              />
            </Field>
            <Field label="The picture" hint="send the frame's still">
              <label className="flex h-7 items-center gap-1.5 text-[11px]">
                <input
                  type="checkbox"
                  checked={draft.model.attach_image}
                  onChange={(e) =>
                    patch({ model: { ...draft.model, attach_image: e.target.checked } })
                  }
                />
                look at it
              </label>
            </Field>
          </div>

          {draft.model.attach_image && draft.model.role !== 'vision' && (
            <Callout tone="warn">
              This step sends a picture but is set to the writing model. A text-only model will not
              complain — it will describe nothing at all, confidently.
            </Callout>
          )}

          <TemplateBox
            label="System prompt"
            hint="who the model is being asked to be"
            value={draft.system}
            tokens={tokens}
            rows={8}
            onChange={(system) => patch({ system })}
          />
          <TemplateBox
            label="Prompt"
            hint="what it is being asked, this time"
            value={draft.prompt}
            tokens={tokens}
            rows={10}
            onChange={(prompt) => patch({ prompt })}
          />

          <Field label="What to ask for" hint="the answer's shape, and where each part goes">
            <OutputFields
              fields={draft.outputs}
              writable={draft.writable}
              onChange={(outputs) => patch({ outputs })}
            />
          </Field>

          {draft.retry && (
            <Field
              label="If a field comes back empty"
              hint={`asks again for: ${draft.retry.when_empty.join(', ') || 'nothing'}`}
            >
              <TextArea
                rows={4}
                value={draft.retry.prompt}
                className="font-mono text-[11px]"
                onChange={(e) =>
                  patch({ retry: { ...draft.retry!, prompt: e.target.value } })
                }
              />
            </Field>
          )}
        </>
      ) : (
        <>
          <Callout tone="info">
            {draft.kind === 'comfy'
              ? 'This step runs a ComfyUI workflow. Which workflow, and which of its parameters take what, are set on the Inspector panel.'
              : draft.kind === 'capture'
                ? 'This step keeps the picture a frame is showing as a project asset, so it survives the run.'
                : 'This step turns a frame into a real shot, with its still wired into the workflow.'}
          </Callout>

          {draft.kind !== 'capture' && (
            <TemplateBox
              label={draft.kind === 'comfy' ? 'What the workflow is asked for' : 'The motion prompt'}
              hint="goes into the workflow's prompt parameter"
              value={draft.prompt}
              tokens={tokens}
              rows={4}
              onChange={(prompt) => patch({ prompt })}
            />
          )}

          {draft.kind === 'comfy' && (
            <label className="flex items-center gap-1.5 text-[11px]">
              <input
                type="checkbox"
                checked={draft.reroll_seed}
                onChange={(e) => patch({ reroll_seed: e.target.checked })}
              />
              A reroll varies the seed
            </label>
          )}
          {draft.kind === 'capture' && (
            <label className="flex items-center gap-1.5 text-[11px]">
              <input
                type="checkbox"
                checked={draft.only_if_missing}
                onChange={(e) => patch({ only_if_missing: e.target.checked })}
              />
              Skip when the picture is already kept
            </label>
          )}
        </>
      )}

      <TokenPalette tokens={tokens} />

      <div className="flex items-center gap-2 border-t border-[var(--color-edge)] pt-2">
        <Button
          size="sm"
          disabled={!dirty || saving}
          onClick={() => onSave(draft as Stage)}
        >
          Save
        </Button>
        <Button size="sm" variant="ghost" disabled={!dirty} onClick={() => setDraft(stage)}>
          Undo changes
        </Button>
        <div className="flex-1" />
        <Button
          size="sm"
          variant="ghost"
          disabled={!stage.edited}
          title={
            stage.edited ? 'Go back to the default wording' : 'This step already uses the default'
          }
          onClick={onReset}
        >
          Reset to default
        </Button>
      </div>
    </div>
  )
}
