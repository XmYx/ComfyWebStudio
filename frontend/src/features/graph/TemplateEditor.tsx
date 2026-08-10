/**
 * Editing a template's graph, opened by double-clicking a placed node.
 *
 * There is no separate template editor. Opening a template materialises it back into a real shot — an
 * *editing session* — and hands it to the ordinary canvas, so every step, link, value node, parameter and
 * ComfyUI round trip works exactly as it does anywhere else. What this component adds is the frame around
 * it: where you are, how to save back, and how to leave.
 *
 * Saving writes over the template and re-syncs every instance in the project, so the node you opened
 * reflects the edit the moment you come back to it.
 */

import { useEffect } from 'react'
import { useMutation } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { Project, Shot } from '@/api/types'
import { useStudio } from '@/store/studio'
import { Badge, Button, Empty, Spinner, useToast } from '@/components/ui'
import { ShotCanvas } from './ShotCanvas'

interface Props {
  project: Project
  /** The shot the user came from, for the way back. */
  parentShotName: string
  templateId: string
  /** What the placed node calls itself, so the header matches what was double-clicked. */
  label: string
  onChanged: () => void
  onRunStep: (stepId: string) => void
}

export function TemplateEditor({
  project, parentShotName, templateId, label, onChanged, onRunStep,
}: Props) {
  const toast = useToast()
  const openInstance = useStudio((s) => s.openInstance)

  const session: Shot | undefined = project.shots.find((s) => s.template_edit_id === templateId)

  // Opening is idempotent on the server — a second call returns the session that already exists — so
  // this can safely fire whenever the project has no session for this template yet.
  const open = useMutation({
    mutationFn: () => api.templates.edit(project.id, templateId),
    onSuccess: onChanged,
    onError: (error: ApiError) => {
      toast.push('bad', error.message)
      openInstance(null)
    },
  })

  useEffect(() => {
    if (!session && !open.isPending && !open.isError) open.mutate()
  }, [session, open.isPending, open.isError])  // eslint-disable-line react-hooks/exhaustive-deps

  const save = useMutation({
    mutationFn: () => api.templates.saveShot(project.id, session!.id, {}),
    onSuccess: (template) => {
      toast.push('ok', `Saved — “${template.name}” is now at revision ${template.revision}.`)
      onChanged()
    },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  const leave = async (discard: boolean) => {
    if (discard && session) {
      if (!confirm('Discard this editing session? The template keeps its last saved version.')) return
      try {
        await api.templates.closeSession(project.id, session.id)
      } catch (error) {
        toast.push('bad', (error as ApiError).message)
      }
      onChanged()
    }
    openInstance(null)
  }

  return (
    // h-full matters: React Flow measures its container, and a wrapper that sizes to its own content
    // leaves the canvas zero pixels tall — nodes render, fitView cannot run, and the pane looks empty.
    // The panel this sits in already draws the border and background, so this is a bare flex column.
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-edge)] bg-[var(--color-panel-2)] px-2 py-1.5">
        <Button size="sm" variant="ghost" onClick={() => void leave(false)} title="Back to the shot">
          ‹ {parentShotName}
        </Button>
        <span className="text-xs font-semibold">{label}</span>
        <Badge tone="info">editing a template</Badge>
        <span className="text-[10px] text-[var(--color-ink-dim)]">
          changes reach every shot that placed it
        </span>

        <div className="flex-1" />

        <Button size="sm" variant="ghost" onClick={() => void leave(true)} title="Throw away this session">
          Discard
        </Button>
        <Button
          size="sm"
          variant="primary"
          disabled={!session || save.isPending}
          onClick={() => save.mutate()}
        >
          {save.isPending ? <Spinner /> : null} Save to template
        </Button>
      </div>

      <div className="min-h-0 flex-1">
        {!session ? (
          <Empty title="Opening the template…">
            {open.isError ? 'It could not be opened.' : <Spinner />}
          </Empty>
        ) : (
          <ShotCanvas
            project={project}
            shot={session}
            onChanged={onChanged}
            onRunStep={onRunStep}
          />
        )}
      </div>
    </div>
  )
}
