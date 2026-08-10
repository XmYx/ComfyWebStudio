/**
 * The project's media library.
 *
 * Two kinds of entry, one list. **Imported** media is a file the user brought in; **generated** media was
 * produced by a step and remembers which one, so it can be refreshed from a later run instead of quietly
 * becoming a stale copy. They are deliberately indistinguishable in use — drag either onto a canvas and
 * you get a media source node.
 */

import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { Asset, Project } from '@/api/types'
import { KIND_COLOR, KIND_ICON } from '@/lib/kinds'
import { relativeTime } from '@/lib/format'
import { startDrag } from '@/lib/dnd'
import { Badge, Button, Panel, PanelHeader, Spinner, cx, useToast } from '@/components/ui'
import { ContextMenu, useContextMenu, type MenuItem } from '@/components/ContextMenu'
import { useCommandContext } from '@/features/menu/useCommandContext'

interface Props {
  project: Project
  onChanged: () => void
}

export function AssetLibrary({ project, onChanged }: Props) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const fileInput = useRef<HTMLInputElement>(null)
  const [filter, setFilter] = useState('')
  const commandContext = useCommandContext()
  const contextMenu = useContextMenu()

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['project', project.id] })
    onChanged()
  }

  const upload = useMutation({
    mutationFn: (file: File) => api.media.upload(project.id, file),
    onSuccess: (asset) => { toast.push('ok', `Imported ${asset.name}.`); invalidate() },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  const assets = Object.values(project.assets).filter((asset) =>
    asset.name.toLowerCase().includes(filter.toLowerCase()),
  )

  const assetMenu = (asset: Asset): MenuItem[] => [
    { type: 'header', label: asset.name },
    {
      type: 'action',
      label: 'Refresh from its source',
      disabled: !asset.source,
      onSelect: async () => {
        try {
          await api.media.refresh(project.id, asset.id)
          toast.push('ok', `${asset.name} refreshed.`)
          invalidate()
        } catch (error) {
          toast.push('bad', (error as ApiError).message)
        }
      },
    },
    {
      type: 'action',
      label: 'Rename…',
      onSelect: async () => {
        const name = prompt('Asset name', asset.name)
        if (name && name !== asset.name) {
          await api.media.renameAsset(project.id, asset.id, name)
          invalidate()
        }
      },
    },
    { type: 'separator' },
    {
      type: 'action',
      label: 'Remove from project',
      danger: true,
      onSelect: async () => {
        if (!confirm(`Remove “${asset.name}”?`)) return
        try {
          await api.media.removeAsset(project.id, asset.id)
          invalidate()
        } catch (error) {
          toast.push('bad', (error as ApiError).message)
        }
      },
    },
  ]

  return (
    <Panel className="flex h-full min-h-0 flex-col">
      <PanelHeader
        actions={
          <>
            <input
              ref={fileInput}
              type="file"
              accept="image/*,video/*,audio/*"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0]
                if (file) upload.mutate(file)
                e.target.value = ''
              }}
            />
            <Button
              size="sm"
              variant="ghost"
              disabled={upload.isPending}
              onClick={() => fileInput.current?.click()}
              title="Import media from a file"
            >
              {upload.isPending ? <Spinner /> : '+'} Import
            </Button>
          </>
        }
      >
        Assets
      </PanelHeader>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {!Object.keys(project.assets).length ? (
          <div className="p-2 text-xs leading-relaxed text-[var(--color-ink-dim)]">
            Nothing yet. <b>Import</b> a file, or right-click a step's output and save it as an asset.
          </div>
        ) : (
          <>
            {Object.keys(project.assets).length > 6 && (
              <input
                placeholder="Filter…"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                className="mb-2 w-full rounded border border-[var(--color-edge)] bg-[var(--color-surface)] px-2 py-1 text-xs outline-none"
              />
            )}
            <div className="grid grid-cols-2 gap-1.5">
              {assets.map((asset) => (
                <button
                  key={asset.id}
                  draggable
                  onDragStart={(event) =>
                    startDrag(event, {
                      kind: 'asset', id: asset.id, name: asset.name, mediaKind: asset.kind,
                    })
                  }
                  onContextMenu={(event) => contextMenu.open(event, assetMenu(asset))}
                  title={`${asset.name} — drag onto a canvas to use it`}
                  className="cursor-grab overflow-hidden rounded-md border border-[var(--color-edge)] bg-[var(--color-surface)] text-left active:cursor-grabbing"
                >
                  <div className="flex h-16 items-center justify-center bg-black/40">
                    {asset.thumb ? (
                      <img
                        src={api.media.url(project.id, asset.thumb)}
                        alt=""
                        className="h-full w-full object-contain"
                      />
                    ) : (
                      <span className="text-lg" style={{ color: KIND_COLOR[asset.kind] }}>
                        {KIND_ICON[asset.kind] ?? '•'}
                      </span>
                    )}
                  </div>
                  <div className="px-1.5 py-1">
                    <div className="truncate text-[10px]">{asset.name}</div>
                    <div className="flex items-center gap-1">
                      <span
                        className={cx('truncate text-[9px]', 'text-[var(--color-ink-dim)]')}
                        title={asset.source ? 'Produced by a step' : 'Imported'}
                      >
                        {asset.source ? relativeTime(asset.generated ?? null) : 'imported'}
                      </span>
                      {asset.source && <Badge tone="info">gen</Badge>}
                    </div>
                  </div>
                </button>
              ))}
            </div>
          </>
        )}
      </div>
      <ContextMenu state={contextMenu.menu} onClose={contextMenu.close} context={commandContext} />
    </Panel>
  )
}
