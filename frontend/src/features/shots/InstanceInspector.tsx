/**
 * The inspector for a placed template.
 *
 * Its parameters are the template's promoted controls, and its outputs are the template's promoted output
 * ports resolved back to the artifacts the inner steps actually produced. That resolution is the whole
 * point: a run reports against expanded steps like `inst_x:consume`, which means nothing to someone
 * looking at a node called "Krea" — so it is mapped back onto the port names the node shows.
 */

import { useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type {
  PlacedTemplate, Project, Shot, TemplateInstance,
} from '@/api/types'
import { useStudio } from '@/store/studio'
import { belongsToInstance, outputsForInstance } from '@/lib/instances'
import { ParamWidget } from '@/features/params/ParamForm'
import { ArtifactPreview } from '@/features/preview/Preview'
import {
  Badge, Button, Callout, Field, Panel, PanelHeader, TextInput, cx, useToast,
} from '@/components/ui'

interface Props {
  project: Project
  shot: Shot
  instance: TemplateInstance
  placed: PlacedTemplate | undefined
  onChanged: () => void
  onRun: (stepIds: string[]) => void
}

type Tab = 'params' | 'output' | 'settings'

export function InstanceInspector({ project, shot, instance, placed, onChanged, onRun }: Props) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [tab, setTab] = useState<Tab>('params')
  const liveSteps = useStudio((s) => s.liveSteps)
  const openInstance = useStudio((s) => s.openInstance)

  const title = instance.name || placed?.summary?.name || 'Template'
  const controls = placed?.controls ?? []

  // The expanded steps this instance owns, so "Run" can target exactly them.
  const innerStepIds = useMemo(
    () => Object.keys(liveSteps).filter((id) => belongsToInstance(id, instance.id)),
    [liveSteps, instance.id],
  )

  const outputs = useMemo(
    () => outputsForInstance(instance, placed?.ports, liveSteps),
    [instance, placed, liveSteps],
  )
  const produced = outputs.filter((entry) => entry.artifact).map((entry) => entry.artifact!)

  const patch = async (body: Partial<TemplateInstance>) => {
    try {
      await api.instances.update(project.id, instance.id, body)
      onChanged()
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    }
  }

  if (placed?.missing) {
    return (
      <Panel className="p-3">
        <Callout tone="bad" title="Missing template">
          This node points at a template that is no longer in the library.
        </Callout>
      </Panel>
    )
  }

  const tabs: Array<[Tab, string]> = [
    ['params', 'Controls'],
    ['output', `Output${produced.length ? ` (${produced.length})` : ''}`],
    ['settings', 'Template'],
  ]

  return (
    <Panel className="flex min-h-0 flex-col">
      <PanelHeader
        actions={
          <>
            <Button
              size="sm"
              disabled={!shot}
              onClick={() => onRun(innerStepIds)}
              title="Run the steps inside this template"
            >
              ▶ Run
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => openInstance(instance.id)}
              title="Look inside this template"
            >
              Open
            </Button>
          </>
        }
      >
        {title}
      </PanelHeader>

      {placed?.stale && (
        <div className="border-b border-[var(--color-edge)] p-2">
          <Callout tone="warn" title="Its template has changed">
            <Button size="sm" onClick={() => void syncNow()}>Update this instance</Button>
          </Callout>
        </div>
      )}

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
        {tab === 'params' && (
          <div className="space-y-3 p-3">
            {!controls.length && (
              <div className="text-xs leading-relaxed text-[var(--color-ink-dim)]">
                This template shows no controls. Open the template library to expose some of the
                parameters it carries.
              </div>
            )}
            {controls.map((control) =>
              control.spec ? (
                <div key={control.key}>
                  <div className="mb-1 text-xs font-medium" title={control.key}>
                    {control.label || control.key}
                  </div>
                  <ParamWidget
                    param={control.spec}
                    value={instance.param_overrides[control.key] ?? control.spec.default}
                    onChange={(value) => void patch({ param_overrides: { [control.key]: value } })}
                  />
                </div>
              ) : null,
            )}
          </div>
        )}

        {tab === 'output' && (
          <div className="p-3">
            {!outputs.length ? (
              <div className="text-xs text-[var(--color-ink-dim)]">
                This template exposes no output ports.
              </div>
            ) : (
              <div className="space-y-3">
                {outputs.map(({ port, artifact }) => (
                  <div key={port.key}>
                    <div className="mb-1 flex items-center justify-between gap-2">
                      <span className="truncate text-xs font-medium">
                        {port.label || port.key}
                      </span>
                      <Badge tone={artifact ? 'ok' : 'muted'}>{port.kind}</Badge>
                    </div>
                    {artifact ? (
                      <ArtifactPreview projectId={project.id} artifacts={[artifact]} />
                    ) : (
                      <div className="rounded border border-dashed border-[var(--color-edge)] px-2 py-3 text-center text-[10px] text-[var(--color-ink-dim)]">
                        Nothing produced yet — run this template.
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {tab === 'settings' && (
          <div className="space-y-3 p-3">
            <Field label="Name on this canvas" hint="leave empty to use the template's own name">
              <TextInput
                defaultValue={instance.name}
                placeholder={placed?.summary?.name ?? 'Template'}
                onBlur={(e) => e.target.value !== instance.name && patch({ name: e.target.value })}
              />
            </Field>

            <div className="flex items-center justify-between rounded-md border border-[var(--color-edge)] px-3 py-2">
              <span className="text-xs">Enabled</span>
              <Button size="sm" variant="ghost" onClick={() => void patch({ enabled: !instance.enabled })}>
                <Badge tone={instance.enabled ? 'ok' : 'muted'}>
                  {instance.enabled ? 'yes' : 'no'}
                </Badge>
              </Button>
            </div>

            <div className="rounded-md border border-[var(--color-edge)] p-2 text-[10px] leading-relaxed text-[var(--color-ink-dim)]">
              <div>Template: {placed?.summary?.name ?? instance.template_id}</div>
              <div>
                Revision {instance.template_revision}
                {placed?.summary && placed.summary.revision !== instance.template_revision
                  ? ` · library is at ${placed.summary.revision}`
                  : ' · up to date'}
              </div>
              <div>{placed?.summary?.step_count ?? 0} step(s) inside</div>
            </div>

            <Button
              variant="danger"
              size="sm"
              onClick={async () => {
                if (!confirm(`Remove “${title}” from this shot?`)) return
                await api.instances.remove(project.id, instance.id)
                onChanged()
              }}
            >
              Remove from shot
            </Button>
          </div>
        )}
      </div>
    </Panel>
  )

  async function syncNow() {
    try {
      const result = await api.instances.sync(project.id, instance.id)
      toast.push('ok', result.changes.length ? result.changes.join('; ') : 'Already up to date.')
      queryClient.invalidateQueries({ queryKey: ['placed', project.id] })
      onChanged()
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    }
  }
}

