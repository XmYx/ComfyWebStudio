import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { Project, SeedMode, Shot, Step } from '@/api/types'
import { useStudio } from '@/store/studio'
import { ParamForm } from '@/features/params/ParamForm'
import { ArtifactPreview } from '@/features/preview/Preview'
import { ElementHistory } from '@/features/history/HistoryDialog'
import {
  Badge, Button, Callout, Field, Modal, Panel, PanelHeader, Select, TextInput, cx, useToast,
} from '@/components/ui'

interface Props {
  project: Project
  shot: Shot
  step: Step
  onChanged: () => void
  onRunStep: (stepId: string, mode: 'step' | 'chain') => void
}

type Tab = 'params' | 'output' | 'history' | 'settings'

/** Short: a tick should feel immediate, it only exists to coalesce a burst of them. */
const PINNED_SAVE_DEBOUNCE_MS = 250

export function StepInspector({ project, shot, step, onChanged, onRunStep }: Props) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [tab, setTab] = useState<Tab>('params')
  const [exposing, setExposing] = useState(false)

  const live = useStudio((s) => s.liveSteps[step.id])
  const workflow = project.workflows[step.workflow_id]

  // Input ports fed by a link are read-only: their value arrives from upstream at run time.
  const linkedKeys = new Set(shot.links.filter((l) => l.to_step === step.id).map((l) => l.to_port))

  const saveParams = useMutation({
    mutationFn: (overrides: Record<string, unknown>) =>
      api.steps.update(project.id, step.id, { param_overrides: overrides }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['project', project.id] }),
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  // Which parameters are pinned to the canvas node, held locally and re-seeded only when a different
  // step is selected. The ref is what a toggle reads: state updates are batched, so two boxes ticked in
  // the same tick would both compute their new list from the same stale one and the second would drop
  // the first. The save is debounced for the same reason — a burst of ticks becomes one request, which
  // also removes any chance of two in-flight PATCHes landing out of order.
  const [pinned, setPinned] = useState<string[]>(step.exposed_params)
  const pinnedRef = useRef(step.exposed_params)
  const pinnedSave = useRef<number>()

  useEffect(() => {
    pinnedRef.current = step.exposed_params
    setPinned(step.exposed_params)
  }, [step.id])  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => window.clearTimeout(pinnedSave.current), [])

  const togglePinned = (key: string, next: boolean) => {
    // Order matters — it is the order they appear on the node — so append rather than rebuild from the
    // workflow's own ordering.
    const without = pinnedRef.current.filter((k) => k !== key)
    const keys = next ? [...without, key] : without
    pinnedRef.current = keys
    setPinned(keys)

    window.clearTimeout(pinnedSave.current)
    pinnedSave.current = window.setTimeout(() => {
      api.steps
        .update(project.id, step.id, { exposed_params: pinnedRef.current })
        .then(() => queryClient.invalidateQueries({ queryKey: ['project', project.id] }))
        .catch((error: ApiError) => toast.push('bad', error.message))
    }, PINNED_SAVE_DEBOUNCE_MS)
  }

  const pinnedKeys = useMemo(() => new Set(pinned), [pinned])

  if (!workflow) {
    return (
      <Panel className="p-3">
        <Callout tone="bad" title="Missing workflow">
          This step points at a workflow that is no longer in the project.
        </Callout>
      </Panel>
    )
  }

  const tabs: Array<[Tab, string]> = [
    ['params', 'Parameters'],
    ['output', `Output${live?.outputs?.length ? ` (${live.outputs.length})` : ''}`],
    ['history', 'History'],
    ['settings', 'Step'],
  ]

  return (
    <Panel className="flex min-h-0 flex-col">
      <PanelHeader
        actions={
          <>
            <Button size="sm" onClick={() => onRunStep(step.id, 'step')} title="Run this step and its dependencies">
              ▶ Run
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => onRunStep(step.id, 'chain')}
              title="Run this step and everything downstream of it"
            >
              ▶▶ From here
            </Button>
          </>
        }
      >
        {step.name}
      </PanelHeader>

      <div className="flex border-b border-[var(--color-edge)]">
        {tabs.map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={cx(
              'flex-1 px-2 py-1.5 text-xs transition-colors',
              tab === key
                ? 'border-b-2 border-[var(--color-accent)] text-[var(--color-ink)]'
                : 'text-[var(--color-ink-dim)] hover:text-[var(--color-ink)]',
            )}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {live?.error && (
          <div className="p-3">
            <Callout tone="bad" title="This step failed">{live.error}</Callout>
          </div>
        )}

        {tab === 'params' && (
          <>
            <div className="border-b border-[var(--color-edge)] px-3 py-2">
              <Button size="sm" variant="ghost" onClick={() => setExposing(true)}>
                + Expose a node parameter
              </Button>
            </div>
            <ParamForm
              params={workflow.params}
              overrides={step.param_overrides}
              linkedKeys={linkedKeys}
              pinnedKeys={pinnedKeys}
              onTogglePinned={togglePinned}
              onChange={(overrides) => saveParams.mutate(overrides)}
              onReset={async () => {
                await api.steps.replaceParams(project.id, step.id, {})
                onChanged()
              }}
              onUnexpose={async (key) => {
                try {
                  await api.workflows.unexpose(project.id, workflow.id, key)
                  queryClient.invalidateQueries({ queryKey: ['project', project.id] })
                } catch (error) {
                  toast.push('bad', (error as ApiError).message)
                }
              }}
            />
          </>
        )}

        {tab === 'output' && (
          <ArtifactPreview projectId={project.id} artifacts={live?.outputs ?? []} />
        )}

        {tab === 'history' && (
          <ElementHistory projectId={project.id} scope="step" targetId={step.id} name={step.name} />
        )}

        {tab === 'settings' && (
          <StepSettings project={project} step={step} onChanged={onChanged} />
        )}
      </div>

      <ExposeModal
        open={exposing}
        onClose={() => setExposing(false)}
        projectId={project.id}
        workflowId={workflow.id}
      />
    </Panel>
  )
}

function StepSettings({
  project, step, onChanged,
}: { project: Project; step: Step; onChanged: () => void }) {
  const toast = useToast()
  const [name, setName] = useState(step.name)

  const save = async (patch: Partial<Step>) => {
    try {
      await api.steps.update(project.id, step.id, patch)
      onChanged()
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    }
  }

  return (
    <div className="space-y-3 p-3">
      <Field label="Name">
        <TextInput
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => name !== step.name && save({ name })}
        />
      </Field>

      <Field label="Seed mode" hint="applies to seed parameters">
        <Select
          value={step.seed_mode ?? ''}
          onChange={(e) => save({ seed_mode: (e.target.value || null) as SeedMode | null })}
        >
          <option value="">Use project default</option>
          <option value="fixed">Fixed</option>
          <option value="randomize">Randomize each run</option>
          <option value="increment">Increment each run</option>
        </Select>
      </Field>

      <Field label="Run on" hint="overrides the project backend">
        <Select
          value={step.backend_id ?? ''}
          onChange={(e) => save({ backend_id: e.target.value || null })}
        >
          <option value="">Project default</option>
          {/* Backend names come from settings; ids are stable so a missing one still round-trips. */}
        </Select>
      </Field>

      <div className="flex items-center justify-between rounded-md border border-[var(--color-edge)] px-3 py-2">
        <span className="text-xs">Enabled</span>
        <Badge tone={step.enabled ? 'ok' : 'muted'}>{step.enabled ? 'yes' : 'no'}</Badge>
      </div>

      <Button
        variant="danger"
        size="sm"
        onClick={async () => {
          if (!confirm(`Remove step “${step.name}”? Its links will be removed too.`)) return
          await api.steps.remove(project.id, step.id)
          onChanged()
        }}
      >
        Delete step
      </Button>
    </div>
  )
}

function ExposeModal({
  open, onClose, projectId, workflowId,
}: { open: boolean; onClose: () => void; projectId: string; workflowId: string }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [filter, setFilter] = useState('')

  const { data, isLoading, error } = useQuery({
    queryKey: ['bindable', projectId, workflowId],
    queryFn: () => api.workflows.bindable(projectId, workflowId),
    enabled: open,
  })

  const candidates = (data ?? []).filter((widget) =>
    `${widget.title} ${widget.input_name}`.toLowerCase().includes(filter.toLowerCase()),
  )

  return (
    <Modal open={open} onClose={onClose} title="Expose a node parameter" width="max-w-2xl">
      <p className="mb-3 text-xs text-[var(--color-ink-dim)]">
        Make any widget in this workflow editable from ComfyWebStudio, without adding WebStudio input nodes
        to the graph.
      </p>

      {error ? (
        <Callout tone="bad">
          {(error as ApiError).message} — this needs a reachable ComfyUI to read node definitions.
        </Callout>
      ) : isLoading ? (
        <div className="py-6 text-center text-xs text-[var(--color-ink-dim)]">Loading…</div>
      ) : (
        <>
          <TextInput
            placeholder="Filter by node or widget…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="mb-3"
          />
          <div className="max-h-96 space-y-1 overflow-y-auto">
            {candidates.map((widget) => (
              <div
                key={widget.key}
                className="flex items-center gap-2 rounded-md border border-[var(--color-edge)] px-2 py-1.5"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs">
                    <span className="text-[var(--color-ink-dim)]">{widget.title}</span>
                    {' · '}
                    <span className="font-medium">{widget.input_name}</span>
                  </div>
                  <div className="truncate text-[10px] text-[var(--color-ink-dim)]">
                    {widget.kind} · currently {JSON.stringify(widget.current)}
                  </div>
                </div>
                {widget.exposed ? (
                  <Badge tone="ok">exposed</Badge>
                ) : (
                  <Button
                    size="sm"
                    onClick={async () => {
                      try {
                        await api.workflows.expose(projectId, workflowId, widget.node_id, widget.input_name)
                        queryClient.invalidateQueries({ queryKey: ['project', projectId] })
                        queryClient.invalidateQueries({ queryKey: ['bindable', projectId, workflowId] })
                        toast.push('ok', `Exposed ${widget.input_name}.`)
                      } catch (err) {
                        toast.push('bad', (err as ApiError).message)
                      }
                    }}
                  >
                    Expose
                  </Button>
                )}
              </div>
            ))}
            {!candidates.length && (
              <div className="py-6 text-center text-xs text-[var(--color-ink-dim)]">
                Nothing matches that filter.
              </div>
            )}
          </div>
        </>
      )}
    </Modal>
  )
}
