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

interface LayoutState {
  showLeftPanel: boolean
  showInspector: boolean
  compactMode: boolean

  dialog: DialogId | null
  clipboard: Clipboard | null
  canvas: CanvasApi | null

  toggleLeftPanel: () => void
  toggleInspector: () => void
  toggleCompact: () => void
  resetLayout: () => void

  openDialog: (id: DialogId) => void
  closeDialog: () => void

  setClipboard: (clipboard: Clipboard | null) => void
  setCanvas: (api: CanvasApi | null) => void
}

const DEFAULTS = { showLeftPanel: true, showInspector: true, compactMode: false }

export const useLayout = create<LayoutState>()(
  persist(
    (set) => ({
      ...DEFAULTS,
      dialog: null,
      clipboard: null,
      canvas: null,

      toggleLeftPanel: () => set((s) => ({ showLeftPanel: !s.showLeftPanel })),
      toggleInspector: () => set((s) => ({ showInspector: !s.showInspector })),
      toggleCompact: () => set((s) => ({ compactMode: !s.compactMode })),
      resetLayout: () => set({ ...DEFAULTS }),

      openDialog: (dialog) => set({ dialog }),
      closeDialog: () => set({ dialog: null }),

      setClipboard: (clipboard) => set({ clipboard }),
      setCanvas: (canvas) => set({ canvas }),
    }),
    {
      name: 'comfywebstudio.layout',
      // Only the panel preferences are worth remembering; a stale dialog or a dangling canvas handle
      // from a previous session would be actively wrong.
      partialize: (state) => ({
        showLeftPanel: state.showLeftPanel,
        showInspector: state.showInspector,
        compactMode: state.compactMode,
      }),
    },
  ),
)
