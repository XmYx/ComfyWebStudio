/**
 * The premise, and which workflows do the work.
 *
 * The bindings are the part that cannot be guessed. Two text-to-image workflows may both take a prompt
 * and call the parameter anything at all, so the app asks once and records the answer — a generation that
 * runs happily while ignoring every prompt is worse than one that refuses to start.
 *
 * The reference-image question is asked here too, because it is the one that most often has no good
 * answer: a workflow with a single image input can still animate a still, it simply has nowhere to be
 * told what a character looks like. That is reported rather than silently dropped.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { Project, Storyboard } from '@/api/types'
import {
  Button, Callout, Field, Panel, PanelHeader, Select, Spinner, TextArea, TextInput, useToast,
} from '@/components/ui'

interface Props {
  project: Project
  board: Storyboard
  onChanged: () => void
}

export function StoryboardSetup({ project, board, onChanged }: Props) {
  const toast = useToast()
  const queryClient = useQueryClient()

  const { data: surfaces } = useQuery({
    queryKey: ['board-surfaces', project.id, board.id, board.binding, board.characters],
    queryFn: () => api.storyboards.surfaces(project.id, board.id),
  })

  const patch = async (body: Parameters<typeof api.storyboards.update>[2]) => {
    try {
      await api.storyboards.update(project.id, board.id, body)
      queryClient.invalidateQueries({ queryKey: ['board-surfaces'] })
      onChanged()
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    }
  }

  /**
   * Only the field that changed.
   *
   * Spreading `board.binding` here looked harmless and was not: the prop only refreshes after the PATCH
   * round-trips and the query refetches, so picking two of these in quick succession sent the *second*
   * one alongside a stale copy of the first — and the first silently reverted to blank. Choosing the
   * prompt and then the starting image lost the prompt every time. The server merges what it is given.
   */
  const bind = (patchBinding: Partial<Storyboard['binding']>) => patch({ binding: patchBinding })

  const write = useMutation({
    mutationFn: () => api.storyboards.write(project.id, board.id, {}),
    onSuccess: (updated) => {
      toast.push('ok', `Wrote ${updated.frames.length} shot(s).`)
      onChanged()
    },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  const workflows = Object.values(project.workflows)

  return (
    <Panel className="flex h-full min-h-0 flex-col overflow-hidden">
      <PanelHeader
        actions={
          <Button size="sm" disabled={write.isPending || !board.premise.trim()} onClick={() => write.mutate()}>
            {write.isPending ? <Spinner /> : null} Write the shots
          </Button>
        }
      >
        {board.name}
      </PanelHeader>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        <Field label="Premise" hint="the idea, in your own words — everything else comes from this">
          <TextArea
            rows={5}
            defaultValue={board.premise}
            placeholder="A lighthouse keeper sees a light answering hers from the sea…"
            onBlur={(e) => e.target.value !== board.premise && patch({ premise: e.target.value })}
          />
        </Field>

        <Field label="House style" hint="added to every image prompt">
          <TextInput
            defaultValue={board.style}
            placeholder="grainy 16mm, muted palette, natural light"
            onBlur={(e) => e.target.value !== board.style && patch({ style: e.target.value })}
          />
        </Field>

        <div className="border-t border-[var(--color-edge)] pt-3">
          <div className="mb-2 text-[10px] font-semibold uppercase tracking-wide text-[var(--color-ink-dim)]">
            Which workflow draws the frames
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Text to image">
              <Select
                value={board.binding.image_workflow_id ?? ''}
                onChange={(e) => bind({ image_workflow_id: e.target.value || null })}
              >
                <option value="">Choose…</option>
                {workflows.map((workflow) => (
                  <option key={workflow.id} value={workflow.id}>{workflow.name}</option>
                ))}
              </Select>
            </Field>
            <Field label="Its prompt">
              <Select
                value={board.binding.image_prompt_param}
                onChange={(e) => bind({ image_prompt_param: e.target.value })}
              >
                <option value="">Choose…</option>
                {(surfaces?.image?.text_params ?? []).map((param) => (
                  <option key={param.key} value={param.key}>{param.label}</option>
                ))}
              </Select>
            </Field>
          </div>
          {(surfaces?.image?.image_ports.length ?? 0) > 0 && (
            <Field
              label="Character reference input"
              hint="where a character's reference image goes, when this workflow takes one"
            >
              <Select
                value={board.binding.image_reference_params[0] ?? ''}
                onChange={(e) =>
                  bind({ image_reference_params: e.target.value ? [e.target.value] : [] })
                }
              >
                <option value="">None</option>
                {(surfaces?.image?.image_ports ?? []).map((port) => (
                  <option key={port.key} value={port.key}>{port.label}</option>
                ))}
              </Select>
            </Field>
          )}
        </div>

        <div className="border-t border-[var(--color-edge)] pt-3">
          <div className="mb-2 text-[10px] font-semibold uppercase tracking-wide text-[var(--color-ink-dim)]">
            Which workflow turns a frame into a shot
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Image to video">
              <Select
                value={board.binding.video_workflow_id ?? ''}
                onChange={(e) => bind({ video_workflow_id: e.target.value || null })}
              >
                <option value="">Choose…</option>
                {workflows.map((workflow) => (
                  <option key={workflow.id} value={workflow.id}>{workflow.name}</option>
                ))}
              </Select>
            </Field>
            <Field label="Its prompt">
              <Select
                value={board.binding.video_prompt_param}
                onChange={(e) => bind({ video_prompt_param: e.target.value })}
              >
                <option value="">Choose…</option>
                {(surfaces?.video?.text_params ?? []).map((param) => (
                  <option key={param.key} value={param.key}>{param.label}</option>
                ))}
              </Select>
            </Field>
            <Field label="Starting image goes to">
              <Select
                value={board.binding.video_image_port}
                onChange={(e) => bind({ video_image_port: e.target.value })}
              >
                <option value="">Choose…</option>
                {(surfaces?.video?.image_ports ?? []).map((port) => (
                  <option key={port.key} value={port.key}>{port.label}</option>
                ))}
              </Select>
            </Field>
            <Field label="Reference goes to" hint="any input the still is not using">
              <Select
                value={board.binding.video_reference_params[0] ?? ''}
                onChange={(e) =>
                  bind({ video_reference_params: e.target.value ? [e.target.value] : [] })
                }
              >
                <option value="">None</option>
                {(surfaces?.spare_video_image_ports ?? []).map((port) => (
                  <option key={port.key} value={port.key}>{port.label}</option>
                ))}
              </Select>
            </Field>
          </div>
        </div>

        {(surfaces?.warnings ?? []).map((warning) => (
          <Callout key={warning} tone="warn">{warning}</Callout>
        ))}
      </div>
    </Panel>
  )
}
