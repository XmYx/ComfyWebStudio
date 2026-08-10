/**
 * Window layout, the internal clipboard, and dialog visibility.
 *
 * Kept separate from `studio.ts` because none of it is about the project — it is about what the
 * application window is currently showing, which is exactly what the Window menu manipulates.
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { Clip, Step } from '@/api/types'
import {
  activateTab, dockAt, dockAtEdge, ensurePlaced, group, groupOf, placedWidgets, removeWidget,
  resizeSplit, split, type DockNode, type DropPosition,
} from './dockTree'

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
  | 'comfy'

export interface WidgetState {
  /** Hidden widgets keep their place in the dock tree, so showing one puts it back where it was. */
  visible: boolean
  /** Popped out of the dock into a window of its own. */
  floating: boolean
  /** Window geometry, in viewport pixels. Only meaningful while floating. */
  rect: { x: number; y: number; w: number; h: number }
  /** Widgets that scroll their own content — a canvas must not be put in a scroll box. */
  noScroll?: boolean
}

/** The layout the app opens with, and what "Reset layout" goes back to. */
export const DEFAULT_WIDGETS: Record<WidgetId, WidgetState> = {
  shots: { visible: true, floating: false, rect: { x: 80, y: 90, w: 320, h: 320 } },
  workflows: { visible: true, floating: false, rect: { x: 120, y: 130, w: 340, h: 420 } },
  assets: { visible: true, floating: false, rect: { x: 160, y: 170, w: 340, h: 420 } },
  canvas: { visible: true, floating: false, rect: { x: 200, y: 120, w: 900, h: 600 }, noScroll: true },
  timeline: { visible: false, floating: false, rect: { x: 220, y: 260, w: 1000, h: 460 }, noScroll: true },
  inspector: { visible: true, floating: false, rect: { x: 640, y: 120, w: 360, h: 560 } },
  monitor: { visible: false, floating: false, rect: { x: 700, y: 160, w: 420, h: 340 } },
  renders: { visible: false, floating: false, rect: { x: 740, y: 200, w: 340, h: 300 } },
  // Off by default: it loads a whole second application, and nobody wants that until they ask for it.
  comfy: { visible: false, floating: false, rect: { x: 240, y: 140, w: 1100, h: 700 }, noScroll: true },
}

/** Panels on the left, the canvas and timeline in the middle, the inspector stack on the right. */
export const defaultTree = (): DockNode =>
  split(
    'row',
    [
      group(['shots', 'workflows', 'assets'], 'shots'),
      group(['canvas', 'timeline', 'comfy'], 'canvas'),
      group(['inspector', 'monitor', 'renders'], 'inspector'),
    ],
    [0.2, 0.55, 0.25],
  )

export const WIDGET_LABELS: Record<WidgetId, string> = {
  shots: 'Shots',
  workflows: 'Workflows',
  assets: 'Assets',
  canvas: 'Canvas',
  inspector: 'Inspector',
  timeline: 'Timeline',
  monitor: 'Monitor',
  renders: 'Renders',
  comfy: 'ComfyUI',
}

interface LayoutState {
  compactMode: boolean

  widgets: Record<WidgetId, WidgetState>
  /** How the docked panels are arranged: nested rows and columns of tab groups. */
  tree: DockNode
  /** A panel filling the whole workspace, hiding the rest until it is restored. */
  maximized: WidgetId | null

  dialog: DialogId | null
  clipboard: Clipboard | null
  canvas: CanvasApi | null

  toggleCompact: () => void
  resetLayout: () => void

  setWidget: (id: WidgetId, patch: Partial<WidgetState>) => void
  toggleWidget: (id: WidgetId) => void
  /** Make a panel visible and frontmost, wherever it lives. Unlike toggling, this only ever shows it. */
  showWidget: (id: WidgetId) => void
  floatWidget: (id: WidgetId, floating: boolean) => void
  /** Drop a widget onto a group: tabbed into it, or split off one of its edges. */
  dockWidget: (id: WidgetId, groupId: string, position: DropPosition) => void
  /** Drop a widget against an outer edge of the workspace, spanning it. */
  dockWidgetToEdge: (id: WidgetId, edge: Exclude<DropPosition, 'centre'>) => void
  setActive: (groupId: string, id: WidgetId) => void
  /** Fill the workspace with one panel, or restore the layout. Passing the maximized one restores. */
  toggleMaximized: (id: WidgetId | null) => void
  /** Drag a splitter: `fraction` is the new share of the child before the boundary. */
  resizeDock: (splitId: string, index: number, fraction: number) => void

  openDialog: (id: DialogId) => void
  closeDialog: () => void

  setClipboard: (clipboard: Clipboard | null) => void
  setCanvas: (api: CanvasApi | null) => void
}

const DEFAULTS = { compactMode: false }

const clone = (widgets: Record<WidgetId, WidgetState>) =>
  Object.fromEntries(
    Object.entries(widgets).map(([id, widget]) => [id, { ...widget, rect: { ...widget.rect } }]),
  ) as Record<WidgetId, WidgetState>

/** Bring a widget to the front of whichever group holds it. */
function activateIn(tree: DockNode, id: WidgetId): DockNode {
  const home = groupOf(tree, id)
  return home ? activateTab(tree, home.id, id) : tree
}

export const useLayout = create<LayoutState>()(
  persist(
    (set) => ({
      ...DEFAULTS,
      widgets: clone(DEFAULT_WIDGETS),
      tree: defaultTree(),
      maximized: null,
      dialog: null,
      clipboard: null,
      canvas: null,

      toggleCompact: () => set((s) => ({ compactMode: !s.compactMode })),
      resetLayout: () =>
        set({ ...DEFAULTS, widgets: clone(DEFAULT_WIDGETS), tree: defaultTree(), maximized: null }),

      setWidget: (id, patch) =>
        set((s) => ({ widgets: { ...s.widgets, [id]: { ...s.widgets[id], ...patch } } })),

      toggleWidget: (id) =>
        set((s) => {
          const visible = !s.widgets[id].visible
          const widgets = { ...s.widgets, [id]: { ...s.widgets[id], visible } }
          // A widget shown again has to be somewhere — one dragged out and hidden has no place left.
          const tree = visible && !widgets[id].floating ? ensurePlaced(s.tree, id) : s.tree
          // And it has to be the front tab, or showing it would appear to do nothing.
          return {
            widgets,
            tree: visible ? activateIn(tree, id) : tree,
            maximized: !visible && s.maximized === id ? null : s.maximized,
          }
        }),

      showWidget: (id) =>
        set((s) => {
          const widgets = { ...s.widgets, [id]: { ...s.widgets[id], visible: true } }
          const tree = widgets[id].floating ? s.tree : activateIn(ensurePlaced(s.tree, id), id)
          return { widgets, tree }
        }),

      floatWidget: (id, floating) =>
        set((s) => {
          const widgets = { ...s.widgets, [id]: { ...s.widgets[id], floating, visible: true } }
          // Floating takes it out of the tree entirely; docking it again gives it a home and the focus.
          const tree = floating ? removeWidget(s.tree, id) : activateIn(ensurePlaced(s.tree, id), id)
          return { widgets, tree, maximized: floating && s.maximized === id ? null : s.maximized }
        }),

      dockWidget: (id, groupId, position) =>
        set((s) => ({
          widgets: { ...s.widgets, [id]: { ...s.widgets[id], floating: false, visible: true } },
          tree: dockAt(s.tree, id, groupId, position),
        })),

      dockWidgetToEdge: (id, edge) =>
        set((s) => ({
          widgets: { ...s.widgets, [id]: { ...s.widgets[id], floating: false, visible: true } },
          tree: dockAtEdge(s.tree, id, edge),
        })),

      setActive: (groupId, id) => set((s) => ({ tree: activateTab(s.tree, groupId, id) })),

      toggleMaximized: (id) =>
        set((s) => ({
          maximized: id === null || s.maximized === id ? null : id,
          // Maximising something hidden would show an empty workspace, so it comes back with it.
          widgets: id && id !== s.maximized
            ? { ...s.widgets, [id]: { ...s.widgets[id], visible: true, floating: false } }
            : s.widgets,
          tree: id && id !== s.maximized ? ensurePlaced(s.tree, id) : s.tree,
        })),

      resizeDock: (splitId, index, fraction) =>
        set((s) => ({ tree: resizeSplit(s.tree, splitId, index, fraction) })),

      openDialog: (dialog) => set({ dialog }),
      closeDialog: () => set({ dialog: null }),

      setClipboard: (clipboard) => set({ clipboard }),
      setCanvas: (canvas) => set({ canvas }),
    }),
    {
      name: 'comfywebstudio.layout',
      // 4: the five fixed dock slots became a tree, so anything older has no layout to restore.
      version: 4,
      // Only the panel preferences are worth remembering; a stale dialog or a dangling canvas handle
      // from a previous session would be actively wrong.
      partialize: (state) => ({
        compactMode: state.compactMode,
        widgets: state.widgets,
        tree: state.tree,
        maximized: state.maximized,
      }),
      /**
       * A stored layout is not trustworthy: it may predate a widget existing, or still place one that has
       * since been removed. Widgets are merged over the current defaults so none can be undefined, and any
       * widget the stored tree does not mention is added back — otherwise a panel added in a new version
       * would be permanently unreachable for anyone with a saved layout.
       */
      merge: (persisted, current) => {
        const saved = (persisted ?? {}) as Partial<LayoutState>
        const widgets = { ...clone(DEFAULT_WIDGETS), ...(saved.widgets ?? {}) }
        let tree = saved.tree ?? defaultTree()
        const placed = new Set(placedWidgets(tree))
        for (const id of Object.keys(widgets) as WidgetId[]) {
          if (!placed.has(id) && !widgets[id].floating) tree = ensurePlaced(tree, id)
        }
        return { ...current, ...saved, widgets, tree }
      },
    },
  ),
)
