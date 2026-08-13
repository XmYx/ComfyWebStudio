/**
 * The storyboard flow every board starts from, for this whole install.
 *
 * The same editor the storyboard's own panel uses, over the layer underneath it. Editing here changes the
 * default; a board that has overridden a step keeps its own version, which is the point of the layering —
 * improving the house prompt should not silently undo somebody's careful rewrite.
 */

import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { Stage, StageView } from '@/api/types'
import { Badge, Button, Callout, Panel, PanelHeader, Spinner, useToast } from '@/components/ui'
import { StageEditor } from '@/features/storyboard/StageEditor'

/**
 * There is no board here, so a token has no value to show — only its name. The board's own panel is where
 * the palette is worth reading; this is where the wording is set.
 */
const TOKEN_NAMES = [
  'board.name', 'board.premise', 'board.premise_brief', 'board.style', 'board.aspect',
  'board.frame_count', 'characters', 'character_names', 'count', 'project.name',
  'frame.id', 'frame.title', 'frame.action', 'frame.camera', 'frame.image_prompt',
  'frame.shot_prompt', 'frame.notes', 'frame.status', 'frame.order', 'frame.number',
  'frame.intent', 'frame.motion', 'frame.characters', 'frame.character_names',
]

const TOKENS = Object.fromEntries(TOKEN_NAMES.map((name) => [name, '']))

export function PipelineSettings() {
  const toast = useToast()
  const [editing, setEditing] = useState<string | null>(null)

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['app-pipeline'],
    queryFn: api.settings.pipeline,
  })

  const save = useMutation({
    mutationFn: (stage: Stage) => api.settings.saveStage(stage),
    onSuccess: () => { void refetch(); toast.push('ok', 'Saved as the default.') },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })
  const reset = useMutation({
    mutationFn: (stageId: string) => api.settings.resetStage(stageId),
    onSuccess: () => void refetch(),
    onError: (error: ApiError) => toast.push('bad', error.message),
  })
  const resetAll = useMutation({
    mutationFn: api.settings.resetPipeline,
    onSuccess: () => { void refetch(); setEditing(null) },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  if (isLoading || !data) return <Panel><div className="p-3"><Spinner /></div></Panel>

  const stage = editing ? data.stages.find((s) => s.id === editing) : null
  const edited = data.stages.filter((s) => s.edited).length

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        actions={
          edited > 0 && !stage ? (
            <Button size="sm" variant="ghost" onClick={() => resetAll.mutate()}>
              Reset all {edited}
            </Button>
          ) : null
        }
      >
        Storyboard flow
      </PanelHeader>

      {stage ? (
        <StageEditor
          key={stage.id}
          stage={stage}
          tokens={TOKENS}
          saving={save.isPending}
          onSave={(next) => save.mutate(next)}
          onReset={() => { reset.mutate(stage.id); setEditing(null) }}
          onClose={() => setEditing(null)}
        />
      ) : (
        <div className="space-y-2 p-3">
          <Callout tone="info">
            These are the steps every new storyboard starts with. A board can change any of them for
            itself — and one that has will keep its own version of that step, whatever is set here.
          </Callout>

          <div className="space-y-1">
            {data.stages.map((entry: StageView, index) => (
              <button
                key={entry.id}
                className="flex w-full items-center gap-2 rounded border border-[var(--color-edge)] px-2 py-1.5 text-left"
                onClick={() => setEditing(entry.id)}
              >
                <span className="w-4 shrink-0 text-[10px] text-[var(--color-ink-dim)]">
                  {index + 1}
                </span>
                <span className="shrink-0 text-xs font-semibold">{entry.name || entry.id}</span>
                <span className="min-w-0 flex-1 truncate text-[10px] text-[var(--color-ink-dim)]">
                  {entry.description}
                </span>
                {entry.edited && <Badge tone="info">changed</Badge>}
                {entry.stale && <Badge tone="warn">default moved on</Badge>}
              </button>
            ))}
          </div>
        </div>
      )}
    </Panel>
  )
}
