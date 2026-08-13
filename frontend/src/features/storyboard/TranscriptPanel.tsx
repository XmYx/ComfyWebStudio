/**
 * What was actually sent, and what actually came back.
 *
 * This is the half of "modular" that matters when something goes wrong. "The model ignored my
 * instruction" and "my instruction never reached the model" look identical from the outside, and the only
 * way to tell them apart is to read the prompt that was sent — so it is kept, and it is here.
 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '@/api/client'
import type { Project, StageRunPreview, Storyboard } from '@/api/types'
import { Badge, Button, Empty, Spinner, cx } from '@/components/ui'

const TONE = {
  success: 'ok',
  error: 'bad',
  skipped: 'muted',
  running: 'info',
} as const

function when(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`
  return new Date(iso).toLocaleDateString()
}

function Exchange({
  project,
  board,
  entry,
}: {
  project: Project
  board: Storyboard
  entry: StageRunPreview
}) {
  const { data: full, isLoading } = useQuery({
    queryKey: ['stage-run', project.id, board.id, entry.id],
    queryFn: () => api.storyboards.stageRun(project.id, board.id, entry.id),
  })

  if (isLoading || !full) return <div className="p-2"><Spinner /></div>

  return (
    <div className="space-y-2 border-t border-[var(--color-edge)] p-2 text-[11px]">
      {full.unknown_tokens.length > 0 && (
        <div className="text-[10px] text-[var(--color-warn)]">
          Sent with unresolved tokens: {full.unknown_tokens.map((t) => `{${t}}`).join(', ')}
        </div>
      )}

      {full.system && (
        <section>
          <Heading>System</Heading>
          <Body>{full.system}</Body>
        </section>
      )}
      <section>
        <Heading>{full.kind === 'comfy' ? 'Prompts sent to the workflow' : 'Prompt'}</Heading>
        <Body>{full.prompt || <em>nothing</em>}</Body>
      </section>
      {full.reply && (
        <section>
          <Heading>Reply</Heading>
          <Body>{full.reply}</Body>
        </section>
      )}
      {full.error && (
        <section>
          <Heading>Error</Heading>
          <div className="text-[var(--color-bad)]">{full.error}</div>
        </section>
      )}

      {full.writes.length > 0 && (
        <section>
          <Heading>Where it went</Heading>
          <div className="space-y-0.5">
            {full.writes.map((write, index) => (
              <div key={index} className="flex items-baseline gap-2">
                <code
                  className={cx(
                    'w-40 shrink-0 truncate',
                    write.applied
                      ? 'text-[var(--color-accent)]'
                      : 'text-[var(--color-ink-dim)] line-through',
                  )}
                >
                  {write.target || 'shown, not saved'}
                </code>
                <span className="min-w-0 flex-1 truncate text-[var(--color-ink-dim)]">
                  {write.applied ? write.after : write.reason || 'not applied'}
                </span>
              </div>
            ))}
          </div>
        </section>
      )}

      {full.truncated && (
        <div className="text-[10px] text-[var(--color-ink-dim)]">
          This record was too long to keep in full and was cut.
        </div>
      )}
    </div>
  )
}

const Heading = ({ children }: { children: React.ReactNode }) => (
  <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-[var(--color-ink-dim)]">
    {children}
  </div>
)

const Body = ({ children }: { children: React.ReactNode }) => (
  <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-[var(--color-surface)] p-1.5 font-mono text-[10px] leading-snug">
    {children}
  </pre>
)

export function TranscriptPanel({
  project,
  board,
  stageId,
  frameId,
}: {
  project: Project
  board: Storyboard
  stageId?: string
  frameId?: string
}) {
  const [open, setOpen] = useState<string | null>(null)
  const { data: entries, isLoading, refetch } = useQuery({
    queryKey: ['board-transcript', project.id, board.id, stageId ?? null, frameId ?? null],
    queryFn: () =>
      api.storyboards.stageRuns(project.id, board.id, {
        stage_id: stageId, frame_id: frameId, limit: 50,
      }),
  })

  if (isLoading) return <div className="p-3"><Spinner /></div>
  if (!entries?.length) {
    return (
      <Empty title="Nothing has run yet">
        Every step records what it sent and what came back. Run one and it will show up here.
      </Empty>
    )
  }

  return (
    <div className="space-y-1 p-2">
      <div className="mb-1 flex items-center gap-2">
        <span className="text-[10px] uppercase tracking-wide text-[var(--color-ink-dim)]">
          {entries.length} exchange{entries.length === 1 ? '' : 's'}
        </span>
        <div className="flex-1" />
        <Button
          size="sm"
          variant="ghost"
          onClick={async () => {
            await api.storyboards.clearStageRuns(project.id, board.id)
            void refetch()
          }}
        >
          Clear
        </Button>
      </div>

      {entries.map((entry) => (
        <div key={entry.id} className="rounded border border-[var(--color-edge)]">
          <button
            className="flex w-full items-center gap-2 px-2 py-1.5 text-left"
            onClick={() => setOpen(open === entry.id ? null : entry.id)}
          >
            <span className="text-[10px] text-[var(--color-ink-dim)]">
              {open === entry.id ? '▾' : '▸'}
            </span>
            <span className="shrink-0 text-[11px] font-semibold">{entry.stage_name}</span>
            {entry.retry && <Badge tone="muted">asked again</Badge>}
            <Badge tone={TONE[entry.status]}>{entry.status}</Badge>
            <span className="min-w-0 flex-1 truncate text-[10px] text-[var(--color-ink-dim)]">
              {entry.error || entry.reply_preview || entry.prompt_preview}
            </span>
            <span className="shrink-0 text-[10px] text-[var(--color-ink-dim)]">
              {entry.model}
              {entry.image_count > 0 ? ' · saw it' : ''} · {when(entry.started)}
            </span>
          </button>
          {open === entry.id && <Exchange project={project} board={board} entry={entry} />}
        </div>
      ))}
    </div>
  )
}
