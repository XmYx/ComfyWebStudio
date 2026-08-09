/**
 * The History panel.
 *
 * Scoped either to the whole project or to one element — a shot, a step, a workflow. Both read the same
 * change log; the element view just filters it. Any entry can be restored, and for an element you can
 * restore *only* that element, leaving everything else where it is.
 */

import { useState } from 'react'
import { useMatch } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { Version, VersionChange } from '@/api/types'
import { useLayout } from '@/store/layout'
import { useStudio } from '@/store/studio'
import { relativeTime } from '@/lib/format'
import {
  Badge, Button, Callout, Checkbox, Modal, Spinner, cx, useToast,
} from '@/components/ui'

const SCOPE_TONE: Record<string, 'ok' | 'warn' | 'bad' | 'info' | 'muted'> = {
  project: 'info', shot: 'info', step: 'ok', link: 'warn',
  workflow: 'info', timeline: 'muted', track: 'muted', clip: 'muted', asset: 'muted',
}

const ACTION_ICON: Record<string, string> = {
  added: '＋', removed: '−', renamed: '✎', edited: '✎', param: '⚙', toggled: '◐',
  connected: '⇢', disconnected: '⇠', moved: '↔', resized: '⤢', trimmed: '✂',
  synced: '⟳', created: '✦', settings: '⚙', exposed: '＋', unexposed: '−',
}

export function HistoryDialog() {
  const open = useLayout((s) => s.dialog === 'history')
  const close = useLayout((s) => s.closeDialog)
  const target = useStudio((s) => s.historyTarget)
  const setTarget = useStudio((s) => s.setHistoryTarget)

  const match = useMatch('/p/:projectId/*')
  const projectId = match?.params.projectId

  const toast = useToast()
  const queryClient = useQueryClient()
  const [includeLayout, setIncludeLayout] = useState(false)
  const [namedOnly, setNamedOnly] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)

  const { data: versions, isLoading, error } = useQuery({
    queryKey: ['versions', projectId, target?.id ?? null, includeLayout, namedOnly],
    queryFn: () =>
      target?.scope === 'shot'
        ? api.versions.forShot(projectId!, target.id)
        : api.versions.list(projectId!, {
            targetId: target?.id,
            includeLayout,
            namedOnly,
            limit: 200,
          }),
    enabled: open && Boolean(projectId),
  })

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['versions', projectId] })
    queryClient.invalidateQueries({ queryKey: ['project', projectId] })
    queryClient.invalidateQueries({ queryKey: ['history', projectId] })
  }

  const restoreAll = useMutation({
    mutationFn: (versionId: string) => api.versions.restore(projectId!, versionId),
    onSuccess: () => { toast.push('ok', 'Project restored.'); refresh() },
    onError: (err: ApiError) => toast.push('bad', err.message),
  })

  const restoreOne = useMutation({
    mutationFn: ({ versionId, scope, id }: { versionId: string; scope: string; id: string }) =>
      api.versions.restoreElement(projectId!, versionId, scope, id),
    onSuccess: () => { toast.push('ok', 'Restored.'); refresh() },
    onError: (err: ApiError) => toast.push('bad', err.message),
  })

  const tag = useMutation({
    mutationFn: (label: string) => api.versions.tag(projectId!, label),
    onSuccess: (version) => { toast.push('ok', `Saved version “${version.label}”.`); refresh() },
    onError: (err: ApiError) => toast.push('bad', err.message),
  })

  const title = target ? `History — ${target.name}` : 'Project history'

  return (
    <Modal
      open={open}
      onClose={() => { close(); setTarget(null) }}
      title={title}
      width="max-w-3xl"
    >
      <div className="mb-3 flex flex-wrap items-center gap-3">
        {target && (
          <Button size="sm" variant="ghost" onClick={() => setTarget(null)}>
            ← Whole project
          </Button>
        )}
        <Checkbox checked={includeLayout} onChange={setIncludeLayout} label="Include moves and resizes" />
        <Checkbox checked={namedOnly} onChange={setNamedOnly} label="Named versions only" />
        <div className="flex-1" />
        <Button
          size="sm"
          onClick={() => {
            const label = prompt('Name this version', '')
            if (label) tag.mutate(label)
          }}
        >
          Save a version…
        </Button>
      </div>

      {error ? (
        <Callout tone="bad">{(error as ApiError).message}</Callout>
      ) : isLoading ? (
        <div className="py-8 text-center text-xs text-[var(--color-ink-dim)]"><Spinner /> Loading…</div>
      ) : !versions?.length ? (
        <div className="rounded-md border border-dashed border-[var(--color-edge)] py-8 text-center text-xs text-[var(--color-ink-dim)]">
          {target
            ? `Nothing has changed on “${target.name}” yet.`
            : 'No changes recorded yet. Every edit from here on will appear in this list.'}
        </div>
      ) : (
        <div className="space-y-1">
          {versions.map((version, index) => (
            <VersionRow
              key={version.id}
              version={version}
              isCurrent={index === 0 && !namedOnly}
              expanded={expanded === version.id}
              onToggle={() => setExpanded(expanded === version.id ? null : version.id)}
              target={target}
              onRestoreAll={() => {
                if (confirm('Roll the whole project back to this point? This itself can be undone.')) {
                  restoreAll.mutate(version.id)
                }
              }}
              onRestoreElement={(scope, id) => restoreOne.mutate({ versionId: version.id, scope, id })}
              onRelabel={async (label) => {
                await api.versions.relabel(projectId!, version.id, label)
                refresh()
              }}
            />
          ))}
        </div>
      )}

      <div className="mt-4 border-t border-[var(--color-edge)] pt-3 text-[11px] text-[var(--color-ink-dim)]">
        Every edit is recorded automatically. Restoring is itself an edit, so it can be undone — you never
        lose the state you were in when you rolled back. Run results are never affected.
      </div>
    </Modal>
  )
}

function VersionRow({
  version, isCurrent, expanded, onToggle, target, onRestoreAll, onRestoreElement, onRelabel,
}: {
  version: Version
  isCurrent: boolean
  expanded: boolean
  onToggle: () => void
  target: { scope: string; id: string; name: string } | null
  onRestoreAll: () => void
  onRestoreElement: (scope: string, id: string) => void
  onRelabel: (label: string | null) => void
}) {
  const scope = version.changes[0]?.scope ?? version.scopes[0] ?? 'project'

  return (
    <div
      className={cx(
        'rounded-md border px-2.5 py-2 transition-colors',
        version.label
          ? 'border-[var(--color-accent)]/50 bg-[var(--color-accent)]/5'
          : 'border-[var(--color-edge)]',
      )}
    >
      <div className="flex items-start gap-2">
        <button onClick={onToggle} className="mt-0.5 w-4 text-[var(--color-ink-dim)]">
          {expanded ? '▾' : '▸'}
        </button>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            {version.label && <Badge tone="info">{version.label}</Badge>}
            <span className="truncate text-xs">{version.summary}</span>
            {isCurrent && <Badge tone="ok">current</Badge>}
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-[10px] text-[var(--color-ink-dim)]">
            <span title={new Date(version.ts).toLocaleString()}>{relativeTime(version.ts)}</span>
            <Badge tone={SCOPE_TONE[scope] ?? 'muted'}>{scope}</Badge>
            {version.changes.length > 1 && <span>{version.changes.length} changes</span>}
          </div>
        </div>

        <div className="flex shrink-0 gap-1">
          <Button
            size="sm"
            variant="ghost"
            title="Name this version so it is easy to find again"
            onClick={() => {
              const label = prompt('Version name', version.label ?? '')
              if (label !== null) onRelabel(label || null)
            }}
          >
            {version.label ? 'Rename' : 'Name'}
          </Button>
          {target ? (
            <Button
              size="sm"
              title={`Restore only ${target.name}, leaving everything else as it is`}
              onClick={() => onRestoreElement(target.scope, target.id)}
            >
              Restore {target.scope}
            </Button>
          ) : (
            <Button size="sm" onClick={onRestoreAll} disabled={isCurrent}>
              Restore
            </Button>
          )}
        </div>
      </div>

      {expanded && (
        <div className="mt-2 space-y-1 border-t border-[var(--color-edge)] pt-2">
          {version.changes.map((change, index) => (
            <ChangeRow
              key={index}
              change={change}
              onRestore={
                ['shot', 'step', 'workflow', 'track', 'timeline'].includes(change.scope)
                  ? () => onRestoreElement(change.scope, change.target_id)
                  : undefined
              }
            />
          ))}
          {!version.changes.length && (
            <div className="text-[11px] text-[var(--color-ink-dim)]">A named checkpoint.</div>
          )}
        </div>
      )}
    </div>
  )
}

function ChangeRow({ change, onRestore }: { change: VersionChange; onRestore?: () => void }) {
  return (
    <div className="group flex items-center gap-2 text-[11px]">
      <span className="w-4 shrink-0 text-center text-[var(--color-ink-dim)]">
        {ACTION_ICON[change.action] ?? '·'}
      </span>
      <span className="min-w-0 flex-1 truncate" title={change.summary}>{change.summary}</span>
      {onRestore && (
        <button
          onClick={onRestore}
          title={`Restore this ${change.scope} to how it was here`}
          className="shrink-0 rounded px-1.5 py-0.5 text-[10px] text-[var(--color-ink-dim)] opacity-0 transition-opacity hover:bg-[var(--color-panel-2)] hover:text-[var(--color-accent)] group-hover:opacity-100"
        >
          restore this {change.scope}
        </button>
      )}
    </div>
  )
}

/** Compact history list for the step inspector's History tab. */
export function ElementHistory({
  projectId, scope, targetId, name,
}: { projectId: string; scope: string; targetId: string; name: string }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const setTarget = useStudio((s) => s.setHistoryTarget)
  const openDialog = useLayout((s) => s.openDialog)

  const { data: versions, isLoading } = useQuery({
    queryKey: ['versions', projectId, targetId, false, false],
    queryFn: () => api.versions.list(projectId, { targetId, limit: 30 }),
  })

  const restore = useMutation({
    mutationFn: (versionId: string) =>
      api.versions.restoreElement(projectId, versionId, scope, targetId),
    onSuccess: () => {
      toast.push('ok', `${name} restored.`)
      queryClient.invalidateQueries({ queryKey: ['project', projectId] })
      queryClient.invalidateQueries({ queryKey: ['versions', projectId] })
    },
    onError: (err: ApiError) => toast.push('bad', err.message),
  })

  if (isLoading) {
    return <div className="p-3 text-xs text-[var(--color-ink-dim)]"><Spinner /> Loading…</div>
  }

  if (!versions?.length) {
    return (
      <div className="p-3 text-xs text-[var(--color-ink-dim)]">
        Nothing has changed on “{name}” yet. Every edit you make will be listed here, and you can put any
        of them back without touching the rest of the project.
      </div>
    )
  }

  return (
    <div className="space-y-1 p-3">
      {versions.map((version) => (
        <div
          key={version.id}
          className="group rounded-md border border-[var(--color-edge)] px-2 py-1.5"
        >
          <div className="flex items-center gap-2">
            <div className="min-w-0 flex-1">
              {version.changes.map((change, index) => (
                <div key={index} className="truncate text-[11px]" title={change.summary}>
                  {ACTION_ICON[change.action] ?? '·'} {change.summary}
                </div>
              ))}
              <div className="mt-0.5 text-[10px] text-[var(--color-ink-dim)]">
                {version.label ? `${version.label} · ` : ''}
                {relativeTime(version.ts)}
              </div>
            </div>
            <Button
              size="sm"
              variant="ghost"
              title={`Put “${name}” back to how it was at this point`}
              onClick={() => restore.mutate(version.id)}
            >
              Restore
            </Button>
          </div>
        </div>
      ))}

      <Button
        size="sm"
        variant="ghost"
        className="mt-2"
        onClick={() => {
          setTarget({ scope: scope as any, id: targetId, name })
          openDialog('history')
        }}
      >
        Open full history…
      </Button>
    </div>
  )
}
