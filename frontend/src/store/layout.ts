/**
 * Window layout, the internal clipboard, and dialog visibility.
 *
 * Kept separate from `studio.ts` because none of it is about the project — it is about what the
 * application window is currently showing, which is exactly what the Window menu manipulates.
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { Clip, Step } from '@/api/types'

export type DialogId =
  | 'shortcuts' | 'about' | 'plugins' | 'newProject' | 'openProject'
  | 'comfyBrowser' | 'buildPlugin' | 'exportProject' | 'history' | 'render' | 'templates'

/** What the shot canvas exposes to the menus. Registered by the canvas itself so the menu stays decoupled. */
export interface CanvasApi {
  zoomIn: () => void
  zoomOut: () => void
  fitView: () => void
  selectAll: () => void
}

export interface Clipboard {
  kind: 'step' | 'clip'
  /** A copy, not a reference — the source can be deleted and the paste still works. */
  payload: Step | Clip
  label: string
}

/**
 * The panels the workspace is made of.
 *
 * Each is a *widget*: docked into one of the edges or the middle by default, but able to be popped out
 * into a floating window. Floating is what lets the shot canvas and the timeline be on screen together —
 * they are both centre widgets, and only one centre widget shows at a time.
 */
export type WidgetId =
  | 'shots' | 'workflows' | 'assets' | 'canvas' | 'inspector' | 'timeline' | 'monitor' | 'renders'

export type DockSlot = 'left' | 'right' | 'top' | 'bottom' | 'centre'

/** Every slot, in the order the workspace lays them out. */
export const DOCK_SLOTS: DockSlot[] = ['top', 'left', 'centre', 'right', 'bottom']

export interface WidgetState {
  /** Where it lives when docked. */
  slot: DockSlot
  /** Hidden widgets keep their slot, so showing one again puts it back where it was. */
  visible: boolean
  /** Popped out of the dock into a window of its own. */
  floating: boolean
  /** Window geometry, in viewport pixels. Only meaningful while floating. */
  rect: { x: number; y: number; w: number; h: number }
  /** Ordering within a dock slot, low first. Also the tab order. */
  order: number
  /** Widgets that scroll their own content — a canvas must not be put in a scroll box. */
  noScroll?: boolean
}

/** The layout the app opens with, and what "Reset layout" goes back to. */
export const DEFAULT_WIDGETS: Record<WidgetId, WidgetState> = {
  shots: { slot: 'left', visible: true, floating: false, rect: { x: 80, y: 90, w: 320, h: 320 }, order: 0 },
  workflows: { slot: 'left', visible: true, floating: false, rect: { x: 120, y: 130, w: 340, h: 420 }, order: 1 },
  assets: { slot: 'left', visible: true, floating: false, rect: { x: 160, y: 170, w: 340, h: 420 }, order: 2 },
  canvas: { slot: 'centre', visible: true, floating: false, rect: { x: 200, y: 120, w: 900, h: 600 }, order: 0, noScroll: true },
  timeline: { slot: 'centre', visible: false, floating: false, rect: { x: 220, y: 260, w: 1000, h: 460 }, order: 1, noScroll: true },
  inspector: { slot: 'right', visible: true, floating: false, rect: { x: 640, y: 120, w: 360, h: 560 }, order: 0 },
  monitor: { slot: 'right', visible: false, floating: false, rect: { x: 700, y: 160, w: 420, h: 340 }, order: 1 },
  renders: { slot: 'right', visible: false, floating: false, rect: { x: 740, y: 200, w: 340, h: 300 }, order: 2 },
}

export const WIDGET_LABELS: Record<WidgetId, string> = {
  shots: 'Shots',
  workflows: 'Workflows',
  assets: 'Assets',
  canvas: 'Canvas',
  inspector: 'Inspector',
  timeline: 'Timeline',
  monitor: 'Monitor',
  renders: 'Renders',
}

interface LayoutState {
  showLeftPanel: boolean
  showInspector: boolean
  compactMode: boolean

  widgets: Record<WidgetId, WidgetState>
  /** The widget on top in each slot. The rest of that slot's widgets are tabs behind it. */
  active: Record<DockSlot, WidgetId | null>

  dialog: DialogId | null
  clipboard: Clipboard | null
  canvas: CanvasApi | null

  toggleLeftPanel: () => void
  toggleInspector: () => void
  toggleCompact: () => void
  resetLayout: () => void

  setWidget: (id: WidgetId, patch: Partial<WidgetState>) => void
  toggleWidget: (id: WidgetId) => void
  floatWidget: (id: WidgetId, floating: boolean) => void
  /** Move a widget into a slot — the drop half of a dock drag. Docks it if it was floating. */
  dockWidget: (id: WidgetId, slot: DockSlot) => void
  setActive: (slot: DockSlot, id: WidgetId) => void

  openDialog: (id: DialogId) => void
  closeDialog: () => void

  setClipboard: (clipboard: Clipboard | null) => void
  setCanvas: (api: CanvasApi | null) => void
}

const DEFAULTS = { showLeftPanel: true, showInspector: true, compactMode: false }

const clone = (widgets: Record<WidgetId, WidgetState>) =>
  Object.fromEntries(
    Object.entries(widgets).map(([id, widget]) => [id, { ...widget, rect: { ...widget.rect } }]),
  ) as Record<WidgetId, WidgetState>

/** The frontmost visible widget of each slot, given a set of widgets. */
function activeForAll(
  widgets: Record<WidgetId, WidgetState>,
  prefer: Partial<Record<DockSlot, WidgetId | null>> = {},
): Record<DockSlot, WidgetId | null> {
  const result = {} as Record<DockSlot, WidgetId | null>
  for (const slot of DOCK_SLOTS) {
    const inSlot = (Object.keys(widgets) as WidgetId[])
      .filter((id) => widgets[id].slot === slot && widgets[id].visible && !widgets[id].floating)
      .sort((a, b) => widgets[a].order - widgets[b].order)
    const preferred = prefer[slot]
    result[slot] = preferred && inSlot.includes(preferred) ? preferred : (inSlot[0] ?? null)
  }
  return result
}

const DEFAULT_ACTIVE = activeForAll(DEFAULT_WIDGETS)

export const useLayout = create<LayoutState>()(
  persist(
    (set) => ({
      ...DEFAULTS,
      widgets: clone(DEFAULT_WIDGETS),
      active: { ...DEFAULT_ACTIVE },
      dialog: null,
      clipboard: null,
      canvas: null,

      toggleLeftPanel: () => set((s) => ({ showLeftPanel: !s.showLeftPanel })),
      toggleInspector: () => set((s) => ({ showInspector: !s.showInspector })),
      toggleCompact: () => set((s) => ({ compactMode: !s.compactMode })),
      resetLayout: () =>
        set({ ...DEFAULTS, widgets: clone(DEFAULT_WIDGETS), active: { ...DEFAULT_ACTIVE } }),

      setWidget: (id, patch) =>
        set((s) => {
          const widgets = { ...s.widgets, [id]: { ...s.widgets[id], ...patch } }
          return { widgets, active: activeForAll(widgets, s.active) }
        }),

      toggleWidget: (id) =>
        set((s) => {
          const widgets = { ...s.widgets, [id]: { ...s.widgets[id], visible: !s.widgets[id].visible } }
          // Showing a widget also brings it to the front of its slot, or it would appear to do nothing.
          const prefer = widgets[id].visible
            ? { ...s.active, [widgets[id].slot]: id }
            : s.active
          return { widgets, active: activeForAll(widgets, prefer) }
        }),

      floatWidget: (id, floating) =>
        set((s) => {
          const widgets = { ...s.widgets, [id]: { ...s.widgets[id], floating, visible: true } }
          // Whatever it leaves behind picks a new front tab; docking it back brings it to the front.
          const prefer = floating ? s.active : { ...s.active, [widgets[id].slot]: id }
          return { widgets, active: activeForAll(widgets, prefer) }
        }),

      dockWidget: (id, slot) =>
        set((s) => {
          // Dropped widgets land last in the slot, which is where a tab you just dragged in belongs.
          const order =
            Math.max(
              -1,
              ...(Object.keys(s.widgets) as WidgetId[])
                .filter((other) => other !== id && s.widgets[other].slot === slot)
                .map((other) => s.widgets[other].order),
            ) + 1
          const widgets = {
            ...s.widgets,
            [id]: { ...s.widgets[id], slot, order, floating: false, visible: true },
          }
          return { widgets, active: activeForAll(widgets, { ...s.active, [slot]: id }) }
        }),

      setActive: (slot, id) => set((s) => ({ active: { ...s.active, [slot]: id } })),

      openDialog: (dialog) => set({ dialog }),
      closeDialog: () => set({ dialog: null }),

      setClipboard: (clipboard) => set({ clipboard }),
      setCanvas: (canvas) => set({ canvas }),
    }),
    {
      name: 'comfywebstudio.layout',
      version: 3,
      // Only the panel preferences are worth remembering; a stale dialog or a dangling canvas handle
      // from a previous session would be actively wrong.
      partialize: (state) => ({
        showLeftPanel: state.showLeftPanel,
        showInspector: state.showInspector,
        compactMode: state.compactMode,
        widgets: state.widgets,
        active: state.active,
      }),
      // A stored layout from before a widget existed would leave it undefined and crash the renderer, so
      // whatever is on disk is merged over the current defaults rather than replacing them. The active
      // tabs are then recomputed, because a stored one may name a widget that is no longer where it was.
      merge: (persisted, current) => {
        const saved = (persisted ?? {}) as Partial<LayoutState>
        const widgets = { ...clone(DEFAULT_WIDGETS), ...(saved.widgets ?? {}) }
        return {
          ...current,
          ...saved,
          widgets,
          active: activeForAll(widgets, saved.active ?? {}),
        }
      },
    },
  ),
)
