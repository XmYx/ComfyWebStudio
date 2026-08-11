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

/**
 * The workspaces, each with its own arrangement of the same panels.
 *
 * Editing shots and cutting a timeline want different things on screen, and a single layout forced one
 * to be a compromise for the other. They are separate workspaces over one set of widgets instead, so a
 * panel can be docked left while editing and along the bottom while cutting without either fighting the
 * other. Which one is showing follows the route.
 */
export type WorkspaceId = 'shots' | 'timeline'

export interface Workspace {
  widgets: Record<WidgetId, WidgetState>
  tree: DockNode
  /** A panel filling this workspace, hiding the rest until it is restored. */
  maximized: WidgetId | null
}

/** Panels on the left, the canvas in the middle, the inspector stack on the right. */
const shotsTree = (): DockNode =>
  split(
    'row',
    [
      group(['shots', 'workflows', 'assets'], 'shots'),
      group(['canvas', 'comfy'], 'canvas'),
      group(['inspector', 'monitor', 'renders'], 'inspector'),
    ],
    [0.2, 0.55, 0.25],
  )

/** What an edit suite looks like: sources left, viewer over the timeline, clip settings right. */
const timelineTree = (): DockNode =>
  split(
    'row',
    [
      group(['shots', 'assets'], 'shots'),
      split('column', [group(['monitor'], 'monitor'), group(['timeline'], 'timeline')], [0.45, 0.55]),
      group(['inspector', 'renders'], 'inspector'),
    ],
    [0.18, 0.57, 0.25],
  )

/** Which panels each workspace opens with. Anything not listed is hidden there. */
const SHOWN: Record<WorkspaceId, WidgetId[]> = {
  shots: ['shots', 'workflows', 'assets', 'canvas', 'inspector'],
  timeline: ['shots', 'assets', 'timeline', 'monitor', 'inspector', 'renders'],
}

function defaultWorkspace(id: WorkspaceId): Workspace {
  const widgets = clone(DEFAULT_WIDGETS)
  for (const key of Object.keys(widgets) as WidgetId[]) {
    widgets[key] = { ...widgets[key], visible: SHOWN[id].includes(key) }
  }
  return {
    widgets,
    tree: id === 'timeline' ? timelineTree() : shotsTree(),
    maximized: null,
  }
}

export const defaultWorkspaces = (): Record<WorkspaceId, Workspace> => ({
  shots: defaultWorkspace('shots'),
  timeline: defaultWorkspace('timeline'),
})

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

  /** Which workspace is on screen. Set by the page, so every action lands on the right layout. */
  workspace: WorkspaceId
  workspaces: Record<WorkspaceId, Workspace>
  setWorkspace: (id: WorkspaceId) => void

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

/**
 * Every layout action edits the workspace that is on screen.
 *
 * Written once as a transform over one workspace, so an action cannot accidentally reach into the other
 * one — which is the whole risk of keeping two layouts in a single store.
 */
function edit(
  state: LayoutState,
  change: (workspace: Workspace) => Workspace,
): Pick<LayoutState, 'workspaces'> {
  const current = state.workspaces[state.workspace]
  return { workspaces: { ...state.workspaces, [state.workspace]: change(current) } }
}

const patchWidget = (workspace: Workspace, id: WidgetId, patch: Partial<WidgetState>): Workspace => ({
  ...workspace,
  widgets: { ...workspace.widgets, [id]: { ...workspace.widgets[id], ...patch } },
})

/** The panels of the workspace on screen. */
export const selectWidgets = (s: LayoutState) => s.workspaces[s.workspace].widgets
/** Its arrangement. */
export const selectTree = (s: LayoutState) => s.workspaces[s.workspace].tree
/** Whichever panel is filling it, if any. */
export const selectMaximized = (s: LayoutState) => s.workspaces[s.workspace].maximized

export const useLayout = create<LayoutState>()(
  persist(
    (set) => ({
      ...DEFAULTS,
      workspace: 'shots',
      workspaces: defaultWorkspaces(),
      dialog: null,
      clipboard: null,
      canvas: null,

      setWorkspace: (workspace) => set({ workspace }),

      toggleCompact: () => set((s) => ({ compactMode: !s.compactMode })),
      resetLayout: () =>
        // Only the one on screen: resetting the timeline because the shot canvas got into a mess would
        // be a surprise.
        set((s) => edit(s, () => defaultWorkspace(s.workspace))),

      setWidget: (id, patch) => set((s) => edit(s, (w) => patchWidget(w, id, patch))),

      toggleWidget: (id) =>
        set((s) =>
          edit(s, (w) => {
            const visible = !w.widgets[id].visible
            const next = patchWidget(w, id, { visible })
            // A widget shown again has to be somewhere — one dragged out and hidden has no place left.
            const placed = visible && !next.widgets[id].floating ? ensurePlaced(w.tree, id) : w.tree
            return {
              ...next,
              // And it has to be the front tab, or showing it would appear to do nothing.
              tree: visible ? activateIn(placed, id) : placed,
              maximized: !visible && w.maximized === id ? null : w.maximized,
            }
          }),
        ),

      showWidget: (id) =>
        set((s) =>
          edit(s, (w) => {
            const next = patchWidget(w, id, { visible: true })
            return {
              ...next,
              tree: next.widgets[id].floating ? w.tree : activateIn(ensurePlaced(w.tree, id), id),
            }
          }),
        ),

      floatWidget: (id, floating) =>
        set((s) =>
          edit(s, (w) => {
            const next = patchWidget(w, id, { floating, visible: true })
            // Floating takes it out of the tree; docking it again gives it a home and the focus.
            return {
              ...next,
              tree: floating ? removeWidget(w.tree, id) : activateIn(ensurePlaced(w.tree, id), id),
              maximized: floating && w.maximized === id ? null : w.maximized,
            }
          }),
        ),

      dockWidget: (id, groupId, position) =>
        set((s) =>
          edit(s, (w) => ({
            ...patchWidget(w, id, { floating: false, visible: true }),
            tree: dockAt(w.tree, id, groupId, position),
          })),
        ),

      dockWidgetToEdge: (id, edge) =>
        set((s) =>
          edit(s, (w) => ({
            ...patchWidget(w, id, { floating: false, visible: true }),
            tree: dockAtEdge(w.tree, id, edge),
          })),
        ),

      setActive: (groupId, id) =>
        set((s) => edit(s, (w) => ({ ...w, tree: activateTab(w.tree, groupId, id) }))),

      toggleMaximized: (id) =>
        set((s) =>
          edit(s, (w) => {
            if (id === null || w.maximized === id) return { ...w, maximized: null }
            // Maximising something hidden would show an empty workspace, so it comes back with it.
            return {
              ...patchWidget(w, id, { visible: true, floating: false }),
              tree: ensurePlaced(w.tree, id),
              maximized: id,
            }
          }),
        ),

      resizeDock: (splitId, index, fraction) =>
        set((s) => edit(s, (w) => ({ ...w, tree: resizeSplit(w.tree, splitId, index, fraction) }))),

      openDialog: (dialog) => set({ dialog }),
      closeDialog: () => set({ dialog: null }),

      setClipboard: (clipboard) => set({ clipboard }),
      setCanvas: (canvas) => set({ canvas }),
    }),
    {
      name: 'comfywebstudio.layout',
      // 5: one layout became one per workspace, so anything older has nothing to restore into.
      version: 5,
      // Only the panel preferences are worth remembering; a stale dialog or a dangling canvas handle
      // from a previous session would be actively wrong. Which workspace was last on screen is not
      // remembered either — the route decides that.
      partialize: (state) => ({
        compactMode: state.compactMode,
        workspaces: state.workspaces,
      }),
      /**
       * A stored layout is not trustworthy: it may predate a widget existing, or still place one that has
       * since been removed. Each workspace's widgets are merged over the current defaults so none can be
       * undefined, and any widget its stored tree does not mention is added back — otherwise a panel added
       * in a new version would be permanently unreachable for anyone with a saved layout.
       */
      merge: (persisted, current) => {
        const saved = (persisted ?? {}) as Partial<LayoutState>
        const workspaces = defaultWorkspaces()

        for (const id of Object.keys(workspaces) as WorkspaceId[]) {
          const stored = saved.workspaces?.[id]
          if (!stored) continue
          const widgets = { ...workspaces[id].widgets, ...(stored.widgets ?? {}) }
          let tree = stored.tree ?? workspaces[id].tree
          const placed = new Set(placedWidgets(tree))
          for (const key of Object.keys(widgets) as WidgetId[]) {
            if (!placed.has(key) && !widgets[key].floating) tree = ensurePlaced(tree, key)
          }
          workspaces[id] = { widgets, tree, maximized: stored.maximized ?? null }
        }

        return { ...current, ...saved, workspaces }
      },
    },
  ),
)
