/**
 * The workspace shell: panels as dockable, splittable, tabbable, floatable widgets.
 *
 * The layout is a tree of splits and tab groups (see `store/dockTree.ts`). Dragging a panel by its tab
 * shows where it would land: the middle of a group tabs it in alongside, an edge of a group splits that
 * group and puts the panel on that side, and the outer rim of the workspace docks it full-width or
 * full-height. Dropping outside the workspace floats it in a window.
 *
 * Two details that are easy to get wrong and matter a lot:
 *
 * * **Height.** Panels are authored as auto-height boxes, and inside a flex cell that collapses them to
 *   their content — for a canvas, to nothing. Every content area forces its child to fill.
 * * **Scrolling.** A docked panel gets a scroll box unless it says it manages its own, because a panel
 *   that cannot scroll simply hides whatever does not fit.
 */

import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from 'react'

import {
  selectMaximized, selectTree, selectWidgets, useLayout, WIDGET_LABELS,
  type WidgetId, type WidgetState,
} from '@/store/layout'
import {
  MIN_SPLIT_FRACTION, visibleTree, type DockNode, type DropPosition,
} from '@/store/dockTree'
import { cx } from '@/components/ui'

const MIN_FLOAT = { w: 240, h: 160 }
const TOP_LIMIT = 8
/** Thickness of a splitter's grab area. Thin enough to look like a seam, thick enough to hit. */
const SPLITTER_PX = 6
/** How close to the workspace rim counts as "dock across the whole thing". */
const RIM_PX = 26
/** How much of a group's width or height each edge drop zone claims. */
const GROUP_EDGE_FRACTION = 0.3
/** How far the pointer must move before a press on a tab counts as a drag rather than a click. */
const DRAG_THRESHOLD_PX = 5

/**
 * What a widget's own root element is forced to, once it is inside a dock frame.
 *
 * `h-full` because panels are authored auto-height and would otherwise collapse; the rest strips the
 * border, rounding and background a standalone Panel draws, since the group already provides them.
 */
const PANEL_RESET = '[&>*]:h-full [&>*]:rounded-none [&>*]:border-0 [&>*]:bg-transparent'

interface Props {
  render: Partial<Record<WidgetId, ReactNode>>
}

/** Where a drag would land: on a group (tabbed or split off an edge), on the workspace rim, or nowhere. */
type DropTarget =
  | { kind: 'group'; groupId: string; position: DropPosition; rect: DOMRect }
  | { kind: 'edge'; edge: Exclude<DropPosition, 'centre'> }
  | null

interface DragState {
  id: WidgetId
  target: DropTarget
  x: number
  y: number
}

export function Dock({ render }: Props) {
  const widgets = useLayout(selectWidgets)
  const tree = useLayout(selectTree)
  const dockWidget = useLayout((s) => s.dockWidget)
  const dockWidgetToEdge = useLayout((s) => s.dockWidgetToEdge)
  const floatWidget = useLayout((s) => s.floatWidget)
  const setWidget = useLayout((s) => s.setWidget)

  const toggleMaximized = useLayout((s) => s.toggleMaximized)
  const workspace = useRef<HTMLDivElement>(null)
  const [drag, setDrag] = useState<DragState | null>(null)

  const renderable = useCallback(
    (id: WidgetId) => widgets[id]?.visible && !widgets[id].floating && render[id] !== undefined,
    [widgets, render],
  )

  const shown = visibleTree(tree, renderable)
  const floating = (Object.keys(widgets) as WidgetId[]).filter(
    (id) => widgets[id].floating && widgets[id].visible && render[id] !== undefined,
  )

  /**
   * Where a pointer at these coordinates would drop.
   *
   * The rim is checked first: it is a thin band, and it is the only way to dock something across the full
   * width or height once the layout has columns. Otherwise the group under the pointer decides, splitting
   * off whichever edge is nearest and tabbing in when the pointer is nearer the middle.
   */
  const targetAt = useCallback((x: number, y: number): DropTarget => {
    const box = workspace.current?.getBoundingClientRect()
    if (!box || x < box.left || x > box.right || y < box.top || y > box.bottom) return null

    if (x - box.left < RIM_PX) return { kind: 'edge', edge: 'left' }
    if (box.right - x < RIM_PX) return { kind: 'edge', edge: 'right' }
    if (y - box.top < RIM_PX) return { kind: 'edge', edge: 'top' }
    if (box.bottom - y < RIM_PX) return { kind: 'edge', edge: 'bottom' }

    const element = document.elementFromPoint(x, y)?.closest('[data-dock-group]') as HTMLElement | null
    if (!element) return null
    const groupId = element.getAttribute('data-dock-group') as string
    const rect = element.getBoundingClientRect()

    // Aiming at the tab strip is an unambiguous "put it in this group", whatever the geometry says.
    if (document.elementFromPoint(x, y)?.closest('[data-tabstrip]')) {
      return { kind: 'group', groupId, position: 'centre', rect }
    }

    const fx = (x - rect.left) / rect.width
    const fy = (y - rect.top) / rect.height
    // Nearest edge wins, but only if the pointer is actually in that edge's band; otherwise it is a tab.
    const distances: [DropPosition, number][] = [
      ['left', fx], ['right', 1 - fx], ['top', fy], ['bottom', 1 - fy],
    ]
    const [position, distance] = distances.sort((a, b) => a[1] - b[1])[0]
    return {
      kind: 'group',
      groupId,
      position: distance < GROUP_EDGE_FRACTION ? position : 'centre',
      rect,
    }
  }, [])

  /** Begin dragging a widget by its tab or its titlebar. */
  const beginDrag = useCallback(
    (id: WidgetId, event: React.MouseEvent) => {
      // Only the left button drags. A right-click on a tab belongs to whatever context menu wants it.
      if (event.button !== 0) return
      event.preventDefault()

      const from = { x: event.clientX, y: event.clientY }
      let dragging = false

      const move = (e: MouseEvent) => {
        // A click is a press and a release in the same place. Without this threshold every click on a
        // tab would end in a drop, re-docking the panel into its own group and shuffling the tab order
        // under the cursor.
        if (!dragging && Math.hypot(e.clientX - from.x, e.clientY - from.y) < DRAG_THRESHOLD_PX) return
        // Dragging a panel is about where it sits in the layout, so restore the layout to drop it into.
        if (!dragging) toggleMaximized(null)
        dragging = true
        setDrag({ id, x: e.clientX, y: e.clientY, target: targetAt(e.clientX, e.clientY) })
      }

      const up = (e: MouseEvent) => {
        window.removeEventListener('mousemove', move)
        window.removeEventListener('mouseup', up)
        setDrag(null)
        if (!dragging) return

        const target = targetAt(e.clientX, e.clientY)
        if (target?.kind === 'group') {
          dockWidget(id, target.groupId, target.position)
          return
        }
        if (target?.kind === 'edge') {
          dockWidgetToEdge(id, target.edge)
          return
        }
        // Dropped outside the workspace: float it, opening the window where it was let go.
        setWidget(id, {
          rect: {
            x: Math.max(0, e.clientX - 120),
            y: Math.max(TOP_LIMIT, e.clientY - 12),
            w: Math.max(MIN_FLOAT.w, widgets[id].rect.w),
            h: Math.max(MIN_FLOAT.h, widgets[id].rect.h),
          },
        })
        floatWidget(id, true)
      }

      window.addEventListener('mousemove', move)
      window.addEventListener('mouseup', up)
    },
    [targetAt, dockWidget, dockWidgetToEdge, floatWidget, setWidget, toggleMaximized, widgets],
  )

  return (
    <>
      <div ref={workspace} className="relative h-full p-2">
        {shown ? (
          <DockRegion node={shown} render={render} onDrag={beginDrag} />
        ) : (
          <div className="grid h-full place-items-center rounded-lg border border-dashed border-[var(--color-edge)] text-xs text-[var(--color-ink-dim)]">
            Every panel is hidden or floating. Show one from the Window menu.
          </div>
        )}

        {drag && (
          <DropOverlay
            target={drag.target}
            label={WIDGET_LABELS[drag.id]}
            workspace={workspace.current}
          />
        )}
      </div>

      {floating.map((id) => (
        <FloatingWidget key={id} id={id} onDrag={beginDrag}>{render[id]}</FloatingWidget>
      ))}

      {drag && (
        <div
          className="pointer-events-none fixed z-50 rounded bg-[var(--color-accent)] px-2 py-1 text-[10px] text-white shadow-lg"
          style={{ left: drag.x + 12, top: drag.y + 12 }}
        >
          {WIDGET_LABELS[drag.id]}
        </div>
      )}
    </>
  )
}

/** One node of the tree: either a tab group, or a row/column of regions with splitters between them. */
function DockRegion({
  node, render, onDrag,
}: {
  node: DockNode
  render: Props['render']
  onDrag: (id: WidgetId, event: React.MouseEvent) => void
}) {
  if (node.type === 'group') {
    return <DockGroup node={node} render={render} onDrag={onDrag} />
  }
  return <DockSplit node={node} render={render} onDrag={onDrag} />
}

function DockSplit({
  node, render, onDrag,
}: {
  node: Extract<DockNode, { type: 'split' }>
  render: Props['render']
  onDrag: (id: WidgetId, event: React.MouseEvent) => void
}) {
  const resizeDock = useLayout((s) => s.resizeDock)
  const container = useRef<HTMLDivElement>(null)
  const row = node.direction === 'row'

  /**
   * Drag a splitter.
   *
   * The fractions are recomputed from the container's own box on every move rather than accumulated from
   * a delta, so a drag that outruns React — or one that starts after a resize — still tracks the pointer
   * exactly. Only the two children either side of the seam are affected; the rest hold their size.
   */
  const dragSplitter = useCallback(
    (index: number, event: React.MouseEvent) => {
      event.preventDefault()
      event.stopPropagation()
      const box = container.current?.getBoundingClientRect()
      if (!box) return

      const total = row ? box.width : box.height
      if (total <= 0) return
      // Everything before this seam is fixed, so the seam's position is measured from there.
      const offset = node.sizes.slice(0, index).reduce((sum, size) => sum + size, 0)
      const pair = node.sizes[index] + node.sizes[index + 1]

      const move = (e: MouseEvent) => {
        const along = row ? (e.clientX - box.left) / total : (e.clientY - box.top) / total
        const fraction = Math.max(
          MIN_SPLIT_FRACTION,
          Math.min(pair - MIN_SPLIT_FRACTION, along - offset),
        )
        resizeDock(node.id, index, fraction)
      }
      const up = () => {
        window.removeEventListener('mousemove', move)
        window.removeEventListener('mouseup', up)
        document.body.style.cursor = ''
      }
      // Held on the body so the cursor does not flicker back whenever the pointer crosses a panel.
      document.body.style.cursor = row ? 'col-resize' : 'row-resize'
      window.addEventListener('mousemove', move)
      window.addEventListener('mouseup', up)
    },
    [node.id, node.sizes, row, resizeDock],
  )

  return (
    <div
      ref={container}
      className={cx('flex h-full w-full min-h-0 min-w-0', row ? 'flex-row' : 'flex-col')}
    >
      {node.children.map((child, index) => (
        // A fragment, not a wrapper element: anything real here would sit on top of the panel and swallow
        // its clicks, and `display: contents` still leaves a box in the hit-testing tree.
        <Fragment key={child.id}>
          <div
            className="min-h-0 min-w-0"
            // The splitters are taken off the top and the fractions divide what is left, so the children
            // always add up to exactly the container. Grow and shrink are off, so a splitter keeps its
            // thickness instead of being scaled along with the panels.
            style={{
              flex: `0 0 calc(${node.sizes[index]} * (100% - ${(node.children.length - 1) * SPLITTER_PX}px))`,
            }}
          >
            <DockRegion node={child} render={render} onDrag={onDrag} />
          </div>
          {index < node.children.length - 1 && (
            <div
              role="separator"
              aria-orientation={row ? 'vertical' : 'horizontal'}
              title="Drag to resize"
              onMouseDown={(event) => dragSplitter(index, event)}
              className={cx(
                'group shrink-0 bg-transparent transition-colors hover:bg-[var(--color-accent)]/40',
                row ? 'cursor-col-resize' : 'cursor-row-resize',
              )}
              style={row ? { width: SPLITTER_PX } : { height: SPLITTER_PX }}
            />
          )}
        </Fragment>
      ))}
    </div>
  )
}

/** A tab strip plus whichever tab is in front. */
function DockGroup({
  node, render, onDrag,
}: {
  node: Extract<DockNode, { type: 'group' }>
  render: Props['render']
  onDrag: (id: WidgetId, event: React.MouseEvent) => void
}) {
  const widgets = useLayout(selectWidgets)
  const setActive = useLayout((s) => s.setActive)
  const toggleWidget = useLayout((s) => s.toggleWidget)
  const floatWidget = useLayout((s) => s.floatWidget)
  const maximized = useLayout(selectMaximized)
  const toggleMaximized = useLayout((s) => s.toggleMaximized)

  /**
   * Maximising lifts this group over the workspace rather than rendering a different tree.
   *
   * That distinction matters more than it sounds: a panel can own live state its React subtree is the
   * only copy of — the embedded ComfyUI is a whole application in an iframe — and rebuilding the tree
   * around it would remount that frame and throw the user's unsaved graph away. Everything else stays
   * mounted underneath, simply covered.
   */
  const filling = Boolean(maximized && node.tabs.includes(maximized))
  const shown = filling
    ? maximized!
    : node.active && node.tabs.includes(node.active) ? node.active : node.tabs[0]
  const isMaximized = Boolean(shown && maximized === shown)

  return (
    <div
      data-dock-group={node.id}
      className={cx(
        'flex min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border border-[var(--color-edge)] bg-[var(--color-panel)]',
        // Positioned against the workspace, which is the nearest positioned ancestor.
        filling ? 'absolute inset-0 z-20 h-auto' : 'h-full',
      )}
    >
      <div
        data-tabstrip={node.id}
        className="flex shrink-0 items-stretch gap-px overflow-x-auto border-b border-[var(--color-edge)] bg-[var(--color-panel-2)]"
      >
        {node.tabs.map((id) => (
          <button
            key={id}
            // The tab is the drag handle — for a tabbed panel that is what "move it by its titlebar"
            // means. A press that never moves just selects the tab.
            onMouseDown={(event) => { setActive(node.id, id); onDrag(id, event) }}
            onDoubleClick={() => toggleMaximized(id)}
            className={cx(
              'cursor-grab whitespace-nowrap px-2.5 py-1 text-[11px] transition-colors active:cursor-grabbing',
              id === shown
                ? 'bg-[var(--color-panel)] text-[var(--color-ink)]'
                : 'text-[var(--color-ink-dim)] hover:bg-[var(--color-panel)]/60',
            )}
          >
            {WIDGET_LABELS[id]}
          </button>
        ))}
        <div className="flex-1" />
        {shown && (
          <div className="flex items-center gap-0.5 pr-1">
            <StripButton
              title={isMaximized ? 'Restore the layout' : 'Fill the workspace with this panel'}
              onClick={() => toggleMaximized(shown)}
            >
              {isMaximized ? '⤡' : '⤢'}
            </StripButton>
            {!isMaximized && (
              <StripButton title="Pop out into a window" onClick={() => floatWidget(shown, true)}>⧉</StripButton>
            )}
            <StripButton title="Hide this panel" onClick={() => toggleWidget(shown)}>✕</StripButton>
          </div>
        )}
      </div>

      <div
        className={cx(
          'min-h-0 flex-1',
          // The group already draws the frame, so the panel inside drops its own — otherwise every
          // widget sits in a box inside a box.
          PANEL_RESET,
          // A panel that manages its own viewport (a canvas, the timeline) must not be put in a scroll
          // box; everything else gets one, or content that does not fit is simply invisible.
          shown && widgets[shown].noScroll ? 'overflow-hidden' : 'overflow-auto',
        )}
      >
        {shown ? render[shown] : (
          <div className="p-4 text-center text-xs text-[var(--color-ink-dim)]">Drag a panel here.</div>
        )}
      </div>
    </div>
  )
}

/**
 * The zone highlight shown while dragging.
 *
 * Drawn over the actual target group rather than a fixed corner of the screen, because with splits there
 * is no longer a fixed set of places a panel can land — "left" means the left of *that* panel now.
 */
function DropOverlay({
  target, label, workspace,
}: { target: DropTarget; label: string; workspace: HTMLElement | null }) {
  if (!target) {
    return (
      <div className="pointer-events-none absolute inset-0 z-30 flex items-start justify-center pt-2">
        <span className="rounded bg-[var(--color-panel-2)] px-2 py-1 text-[10px] text-[var(--color-ink-dim)]">
          Release outside to float {label}
        </span>
      </div>
    )
  }

  if (target.kind === 'edge') {
    const box: Record<Exclude<DropPosition, 'centre'>, string> = {
      left: 'left-0 top-0 h-full w-[22%]',
      right: 'right-0 top-0 h-full w-[22%]',
      top: 'left-0 top-0 h-[22%] w-full',
      bottom: 'bottom-0 left-0 h-[22%] w-full',
    }
    return (
      <div className="pointer-events-none absolute inset-0 z-30">
        <div className={cx('absolute rounded border-2 border-[var(--color-accent)] bg-[var(--color-accent)]/25', box[target.edge])} />
        <Caption>Dock {label} across the {target.edge}</Caption>
      </div>
    )
  }

  // The group's rect is in viewport coordinates; the overlay is positioned inside the workspace.
  const origin = workspace?.getBoundingClientRect()
  if (!origin) return null
  const { rect, position } = target
  const left = rect.left - origin.left
  const top = rect.top - origin.top

  const half = (fraction: number) => ({
    left: position === 'right' ? left + rect.width * (1 - fraction) : left,
    top: position === 'bottom' ? top + rect.height * (1 - fraction) : top,
    width: position === 'left' || position === 'right' ? rect.width * fraction : rect.width,
    height: position === 'top' || position === 'bottom' ? rect.height * fraction : rect.height,
  })

  const style = position === 'centre'
    ? { left, top, width: rect.width, height: rect.height }
    : half(0.4)

  return (
    <div className="pointer-events-none absolute inset-0 z-30">
      <div
        className="absolute rounded border-2 border-[var(--color-accent)] bg-[var(--color-accent)]/25"
        style={style}
      />
      <Caption>
        {position === 'centre' ? `Tab ${label} in here` : `Dock ${label} to the ${position}`}
      </Caption>
    </div>
  )
}

function Caption({ children }: { children: ReactNode }) {
  return (
    <div className="absolute inset-x-0 top-2 text-center">
      <span className="rounded bg-[var(--color-accent)] px-2 py-1 text-[10px] text-white">{children}</span>
    </div>
  )
}

function FloatingWidget({
  id, children, onDrag,
}: {
  id: WidgetId
  children: ReactNode
  onDrag: (id: WidgetId, event: React.MouseEvent) => void
}) {
  const widget = useLayout((s) => selectWidgets(s)[id])
  const setWidget = useLayout((s) => s.setWidget)
  const floatWidget = useLayout((s) => s.floatWidget)
  const toggleWidget = useLayout((s) => s.toggleWidget)

  const rect = useRef<WidgetState['rect']>(widget.rect)
  rect.current = widget.rect

  /**
   * Moving and resizing a floating window.
   *
   * Listeners live on the window: a pointer moving faster than React re-renders routinely leaves the
   * element, and a window that stops following the mouse feels broken. Geometry is written to the store
   * on release so a drag is one persisted change rather than a hundred.
   */
  const gesture = useCallback(
    (event: React.MouseEvent, mode: 'move' | 'resize') => {
      event.preventDefault()
      const from = { x: event.clientX, y: event.clientY }
      const start = { ...rect.current }
      const element = (event.currentTarget as HTMLElement).closest('[data-floating]') as HTMLElement

      const move = (e: MouseEvent) => {
        const dx = e.clientX - from.x
        const dy = e.clientY - from.y
        rect.current =
          mode === 'move'
            ? {
                x: Math.max(0, Math.min(window.innerWidth - 80, start.x + dx)),
                y: Math.max(TOP_LIMIT, Math.min(window.innerHeight - 40, start.y + dy)),
                w: start.w,
                h: start.h,
              }
            : {
                x: start.x,
                y: start.y,
                w: Math.max(MIN_FLOAT.w, start.w + dx),
                h: Math.max(MIN_FLOAT.h, start.h + dy),
              }
        if (element) {
          element.style.left = `${rect.current.x}px`
          element.style.top = `${rect.current.y}px`
          element.style.width = `${rect.current.w}px`
          element.style.height = `${rect.current.h}px`
        }
      }
      const up = () => {
        window.removeEventListener('mousemove', move)
        window.removeEventListener('mouseup', up)
        setWidget(id, { rect: rect.current })
      }
      window.addEventListener('mousemove', move)
      window.addEventListener('mouseup', up)
    },
    [id, setWidget],
  )

  // A window remembered from a larger screen would otherwise open off the edge, unreachable.
  useEffect(() => {
    const x = Math.min(widget.rect.x, Math.max(0, window.innerWidth - 120))
    const y = Math.min(widget.rect.y, Math.max(TOP_LIMIT, window.innerHeight - 60))
    if (x !== widget.rect.x || y !== widget.rect.y) setWidget(id, { rect: { ...widget.rect, x, y } })
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div
      data-floating={id}
      className="fixed z-40 flex flex-col overflow-hidden rounded-lg border border-[var(--color-accent)]/60 bg-[var(--color-panel)] shadow-2xl"
      style={{ left: widget.rect.x, top: widget.rect.y, width: widget.rect.w, height: widget.rect.h }}
    >
      <div
        className="flex shrink-0 cursor-move select-none items-center gap-2 border-b border-[var(--color-edge)] bg-[var(--color-panel-2)] px-2 py-1"
        // Alt-drag re-docks, using the same gesture the tabs use, so a floating window can be put back
        // anywhere. A plain drag just moves the window, which is what people expect of a titlebar.
        onMouseDown={(event) => (event.altKey ? onDrag(id, event) : gesture(event, 'move'))}
        title="Drag to move · Alt-drag to dock it somewhere"
      >
        <span className="flex-1 truncate text-[11px] font-semibold">{WIDGET_LABELS[id]}</span>
        <StripButton title="Dock it again" onClick={() => floatWidget(id, false)}>⇲</StripButton>
        <StripButton title="Close" onClick={() => toggleWidget(id)}>✕</StripButton>
      </div>

      <div
        className={cx(
          'min-h-0 flex-1',
          PANEL_RESET,
          widget.noScroll ? 'overflow-hidden' : 'overflow-auto',
        )}
      >
        {children}
      </div>

      <div
        title="Resize"
        onMouseDown={(event) => gesture(event, 'resize')}
        className="absolute bottom-0 right-0 h-3 w-3 cursor-nwse-resize bg-[var(--color-accent)]/40"
      />
    </div>
  )
}

function StripButton({
  children, title, onClick,
}: { children: ReactNode; title: string; onClick: () => void }) {
  return (
    <button
      title={title}
      onClick={(event) => { event.stopPropagation(); onClick() }}
      onMouseDown={(event) => event.stopPropagation()}
      className="rounded px-1 text-[10px] text-[var(--color-ink-dim)] hover:text-[var(--color-accent)]"
    >
      {children}
    </button>
  )
}
