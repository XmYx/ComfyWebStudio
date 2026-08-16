/**
 * Asking the model for more shots, and saying where they go.
 *
 * Two shapes of the same thing. The strip's header offers the ends of the sequence — the common case,
 * "this needs an opening" or "it stops too abruptly". Each frame offers *after this one*, which is the
 * only honest way to say "in between": a gap is named by the shot on its left, and pointing at that shot
 * is a great deal more direct than picking two numbers out of a list.
 *
 * The count sits with the button rather than in settings, because how many shots a gap needs is a
 * property of the gap.
 */

import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { Project, Storyboard } from '@/api/types'
import { Button, Spinner, TextInput, cx, useToast } from '@/components/ui'

type Where = 'start' | 'end' | 'after'

interface Props {
  project: Project
  board: Storyboard
  onChanged: () => void
  /** Named when this is the control on a frame; absent for the header's version. */
  afterFrameId?: string
  className?: string
}

export function AddShots({ project, board, onChanged, afterFrameId, className }: Props) {
  const toast = useToast()
  const [count, setCount] = useState(3)
  const [open, setOpen] = useState(false)

  const extend = useMutation({
    mutationFn: (where: Where) =>
      api.storyboards.extend(project.id, board.id, {
        count,
        at: where,
        ...(where === 'after' && afterFrameId ? { after_frame_id: afterFrameId } : {}),
      }),
    onSuccess: (updated, where) => {
      setOpen(false)
      toast.push(
        'ok',
        `Added ${updated.frames.length - board.frames.length} shot(s) ` +
          (where === 'start' ? 'at the start.' : where === 'end' ? 'at the end.' : 'in the middle.'),
      )
      onChanged()
    },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  // Nothing to extend and nothing to sit between: writing the board is the thing to do first.
  if (!board.frames.length) return null

  const number = (
    <TextInput
      type="number"
      min={1}
      max={60}
      value={count}
      aria-label="How many shots to add"
      onChange={(e) => setCount(Math.max(1, Math.min(60, Number(e.target.value) || 1)))}
      className="w-14"
    />
  )

  if (afterFrameId) {
    return open ? (
      <span className={cx('flex items-center gap-1', className)}>
        {number}
        <Button
          size="sm"
          disabled={extend.isPending}
          title={`Write ${count} shot(s) to follow this one`}
          onClick={() => extend.mutate('after')}
        >
          {extend.isPending ? <Spinner /> : null} Add here
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>✕</Button>
      </span>
    ) : (
      <Button
        size="sm"
        variant="ghost"
        title="Write more shots to go after this one, told what comes before and after"
        onClick={() => setOpen(true)}
        className={className}
      >
        + Shots after
      </Button>
    )
  }

  return (
    <span className={cx('flex items-center gap-1', className)}>
      {number}
      <Button
        size="sm"
        variant="ghost"
        disabled={extend.isPending}
        title={`Write ${count} shot(s) to open the sequence`}
        onClick={() => extend.mutate('start')}
      >
        + Start
      </Button>
      <Button
        size="sm"
        variant="ghost"
        disabled={extend.isPending}
        title={`Write ${count} shot(s) to close the sequence`}
        onClick={() => extend.mutate('end')}
      >
        {extend.isPending ? <Spinner /> : null} + End
      </Button>
    </span>
  )
}
