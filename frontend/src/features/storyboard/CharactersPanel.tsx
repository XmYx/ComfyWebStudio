/**
 * The people in the story, and the pictures that say what they look like.
 *
 * A character lives on the storyboard rather than on a frame because they are the thing that has to stay
 * the same *between* frames — which is exactly what their reference images are for. Drop an asset onto a
 * character and it is fed to the workflow's reference input wherever that character appears.
 *
 * Whether the chosen workflow actually has such an input is answered next door, in the setup panel. This
 * one lets you say what the characters are regardless: deciding who is in the film should not depend on
 * having already chosen a workflow.
 */

import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import { useStudioEvents } from '@/api/events'
import type { Project, Storyboard, StoryboardCharacter } from '@/api/types'
import { isOurDrag, readDrag } from '@/lib/dnd'
import {
  Badge, Button, Empty, Panel, PanelHeader, Spinner, TextArea, TextInput, cx, useToast,
} from '@/components/ui'

interface Props {
  project: Project
  board: Storyboard
  onChanged: () => void
}

export function CharactersPanel({ project, board, onChanged }: Props) {
  const toast = useToast()
  const fail = (error: unknown) => toast.push('bad', (error as ApiError).message)

  /** Characters whose reference is being drawn right now, so the row can say so. */
  const [drawing, setDrawing] = useState<Record<string, boolean>>({})

  const suggest = useMutation({
    mutationFn: () => api.storyboards.suggestCharacters(project.id, board.id),
    onSuccess: async (found) => {
      // Proposing is not deciding — but with nothing there yet, accepting them all is the obvious
      // next click, so it is done for you and can be edited or deleted like anything else.
      //
      // Anyone already on the board has been filtered out by the server, which also knows to tell the
      // model who it already has. Asked twice, this used to hand back the whole cast again.
      for (const character of found) {
        await api.storyboards.addCharacter(project.id, board.id, character)
      }
      toast.push('ok', found.length ? `Added ${found.length}.` : 'Nobody new was found.')
      onChanged()
    },
    onError: fail,
  })

  const drawPortrait = async (character: StoryboardCharacter) => {
    setDrawing((busy) => ({ ...busy, [character.id]: true }))
    try {
      await api.storyboards.drawCharacter(project.id, board.id, character.id)
    } catch (error) {
      setDrawing(({ [character.id]: _done, ...rest }) => rest)
      fail(error)
    }
  }

  // The picture is kept as an asset by the server once the run finishes, which is what makes it usable
  // as a reference at all — so the row just waits for the project to change under it.
  useStudioEvents((event) => {
    if (event.type !== 'run.finished' && event.type !== 'project.changed') return
    setDrawing({})
    if (event.type === 'project.changed'
        && (event.data as { action?: string }).action === 'character_reference_drawn') {
      onChanged()
    }
  }, [board.id])

  const patch = async (character: StoryboardCharacter, body: Partial<StoryboardCharacter>) => {
    try {
      await api.storyboards.updateCharacter(project.id, board.id, character.id, body)
      onChanged()
    } catch (error) {
      fail(error)
    }
  }

  const dropAsset = async (character: StoryboardCharacter, event: React.DragEvent) => {
    const payload = readDrag(event)
    if (!payload || payload.kind !== 'asset') return
    event.preventDefault()
    if (character.reference_asset_ids.includes(payload.id)) return
    await patch(character, {
      reference_asset_ids: [...character.reference_asset_ids, payload.id],
    })
  }

  return (
    <Panel className="flex h-full min-h-0 flex-col overflow-hidden">
      <PanelHeader
        actions={
          <>
            <Button
              size="sm"
              variant="ghost"
              disabled={suggest.isPending || !board.premise.trim()}
              title="Read the premise and propose the people in it"
              onClick={() => suggest.mutate()}
            >
              {suggest.isPending ? <Spinner /> : null} Find them
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={async () => {
                await api.storyboards.addCharacter(project.id, board.id, { name: 'New character' })
                onChanged()
              }}
            >
              +
            </Button>
          </>
        }
      >
        Characters
      </PanelHeader>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {!board.characters.length ? (
          <Empty title="Nobody yet">
            Press <strong>Find them</strong> to read the premise, or add one by hand. Write what somebody
            looks like and <strong>Draw them</strong> makes their reference picture; dragging an asset onto
            a character works too.
          </Empty>
        ) : (
          board.characters.map((character) => (
            <div
              key={character.id}
              data-character={character.id}
              onDragOver={(event) => {
                if (!isOurDrag(event)) return
                event.preventDefault()
                event.dataTransfer.dropEffect = 'copy'
              }}
              onDrop={(event) => void dropAsset(character, event)}
              className={cx(
                'mb-2 rounded-md border border-[var(--color-edge)] bg-[var(--color-surface)] p-2',
                'hover:border-[var(--color-accent)]/50',
              )}
            >
              <div className="mb-1 flex items-center gap-2">
                <TextInput
                  defaultValue={character.name}
                  className="h-6 flex-1 text-xs"
                  onBlur={(e) => e.target.value !== character.name && patch(character, { name: e.target.value })}
                />
                <button
                  title="Remove"
                  className="px-1 text-[10px] text-[var(--color-ink-dim)] hover:text-[var(--color-bad)]"
                  onClick={async () => {
                    await api.storyboards.removeCharacter(project.id, board.id, character.id)
                    onChanged()
                  }}
                >
                  ✕
                </button>
              </div>

              <TextArea
                rows={2}
                defaultValue={character.appearance}
                placeholder="what they look like, for the image prompts"
                className="mb-1 text-[11px]"
                onBlur={(e) =>
                  e.target.value !== character.appearance && patch(character, { appearance: e.target.value })
                }
              />

              <div className="flex flex-wrap items-center gap-1">
                {character.reference_asset_ids.map((assetId) => {
                  const asset = project.assets[assetId]
                  if (!asset) return null
                  return (
                    <button
                      key={assetId}
                      title={`${asset.name} — click to remove`}
                      onClick={() =>
                        patch(character, {
                          reference_asset_ids: character.reference_asset_ids.filter((id) => id !== assetId),
                        })
                      }
                      className="rounded border border-[var(--color-edge)] hover:border-[var(--color-bad)]"
                    >
                      <img
                        src={api.media.url(project.id, asset.thumb || asset.path)}
                        alt=""
                        className="h-10 w-14 rounded object-cover"
                      />
                    </button>
                  )
                })}
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={drawing[character.id] || !character.appearance.trim()}
                  title={
                    character.appearance.trim()
                      ? 'Draw them with the text-to-image workflow, from what you wrote above'
                      : 'Write what they look like first'
                  }
                  onClick={() => void drawPortrait(character)}
                >
                  {drawing[character.id] ? <Spinner /> : null}
                  {character.reference_asset_ids.length ? 'Draw another' : 'Draw them'}
                </Button>
                {!character.reference_asset_ids.length && !drawing[character.id] && (
                  <Badge tone="muted">or drag an asset here</Badge>
                )}
              </div>
            </div>
          ))
        )}
      </div>
    </Panel>
  )
}
