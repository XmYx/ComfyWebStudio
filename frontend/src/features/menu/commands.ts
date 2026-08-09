/**
 * The command registry.
 *
 * Menus, keyboard shortcuts and any future command palette all read from this one list, so a command can
 * never be reachable one way but stale another. A command declares whether it is currently applicable
 * (`enabled`) and, for toggles, whether it is on (`checked`); the menu renders both.
 */

import type { NavigateFunction } from 'react-router-dom'
import type { QueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { Clip, Project, RenderRequest, Shot, Step } from '@/api/types'
import { useLayout } from '@/store/layout'
import { useStudio } from '@/store/studio'

export interface CommandContext {
  project: Project | null
  shot: Shot | null
  step: Step | null
  navigate: NavigateFunction
  queryClient: QueryClient
  toast: (tone: 'ok' | 'bad' | 'info', message: string) => void
  history: { undo: number; redo: number }
  refresh: () => void
}

export interface Command {
  id: string
  label: string
  shortcut?: string
  /** Shown in the menu instead of the label when the command is a toggle that is currently on. */
  run: (ctx: CommandContext) => void | Promise<void>
  enabled?: (ctx: CommandContext) => boolean
  checked?: () => boolean
  danger?: boolean
}

const hasProject = (ctx: CommandContext) => ctx.project !== null
const hasStep = (ctx: CommandContext) => ctx.step !== null

function download(url: string, filename?: string) {
  const anchor = document.createElement('a')
  anchor.href = url
  if (filename) anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

async function guard(ctx: CommandContext, action: () => Promise<unknown>, success?: string) {
  try {
    await action()
    if (success) ctx.toast('ok', success)
    ctx.refresh()
  } catch (error) {
    ctx.toast('bad', error instanceof ApiError ? error.message : String(error))
  }
}

/** A timeline with something in it — nothing here can render an empty one. */
const hasTimeline = (ctx: CommandContext) =>
  ctx.project !== null && ctx.project.timeline.duration > 0

/**
 * Start a render and go to the timeline, where the progress bar and the finished file are.
 *
 * The result arrives as a `render.finished` event, so there is nothing to await here beyond the request
 * being accepted — which is also the only part that can fail synchronously.
 */
async function startRender(
  ctx: CommandContext, body: RenderRequest, message: string,
): Promise<void> {
  if (!ctx.project) return
  ctx.navigate(`/p/${ctx.project.id}/timeline`)
  try {
    const result = await api.timeline.render(ctx.project.id, { ...body, time_s: 0 })
    ctx.toast('info', result.outputs > 1 ? `Rendering ${result.outputs} files…` : message)
  } catch (error) {
    ctx.toast('bad', error instanceof ApiError ? error.message : String(error))
  }
}

/** Selected clip, resolved from the timeline selection. */
function selectedClip(project: Project | null): { track: string; clip: Clip } | null {
  const selection = useStudio.getState().selectedClip
  if (!project || !selection) return null
  const track = project.timeline.tracks.find((t) => t.id === selection.trackId)
  const clip = track?.clips.find((c) => c.id === selection.clipId)
  return track && clip ? { track: track.id, clip } : null
}

export const COMMANDS: Command[] = [
  // -- File ------------------------------------------------------------------------------------------
  {
    id: 'file.new',
    label: 'New Project…',
    shortcut: 'Mod+N',
    run: () => useLayout.getState().openDialog('newProject'),
  },
  {
    id: 'file.open',
    label: 'Open Project…',
    shortcut: 'Mod+O',
    run: (ctx) => ctx.navigate('/projects'),
  },
  {
    id: 'file.save',
    label: 'Save Project',
    shortcut: 'Mod+S',
    enabled: hasProject,
    // Every edit is already written to disk as it happens; this flushes and confirms, so Ctrl+S does
    // something honest rather than nothing.
    run: (ctx) =>
      guard(
        ctx,
        () => api.projects.update(ctx.project!.id, { name: ctx.project!.name }),
        'Project saved.',
      ),
  },
  {
    id: 'file.duplicate',
    label: 'Duplicate Project',
    enabled: hasProject,
    run: (ctx) =>
      guard(ctx, async () => {
        const copy = await api.projects.duplicate(ctx.project!.id)
        ctx.navigate(`/p/${copy.id}/shots`)
      }, 'Project duplicated.'),
  },
  {
    id: 'file.importWorkflowComfy',
    label: 'Import Workflow from ComfyUI…',
    shortcut: 'Mod+I',
    enabled: hasProject,
    run: () => useLayout.getState().openDialog('comfyBrowser'),
  },
  {
    id: 'file.importWorkflowFile',
    label: 'Import Workflow from File…',
    enabled: hasProject,
    run: (ctx) =>
      pickFile('.json', (file) =>
        guard(ctx, async () => {
          const workflow = await api.workflows.upload(ctx.project!.id, file)
          ctx.toast('ok', `Imported ${workflow.name} — ${workflow.ports.length} port(s).`)
        }),
      ),
  },
  {
    id: 'file.importAsset',
    label: 'Import Media…',
    enabled: hasProject,
    run: (ctx) =>
      pickFile('image/*,video/*,audio/*', (file) =>
        guard(ctx, () => api.media.upload(ctx.project!.id, file), `Imported ${file.name}.`),
      ),
  },
  {
    id: 'file.importProject',
    label: 'Import Project…',
    run: (ctx) =>
      pickFile('.cwsproj', (file) =>
        guard(ctx, async () => {
          const project = await api.projects.import(file)
          ctx.navigate(`/p/${project.id}/shots`)
        }, 'Project imported.'),
      ),
  },
  {
    id: 'file.exportProject',
    label: 'Export Project…',
    shortcut: 'Mod+E',
    enabled: hasProject,
    run: () => useLayout.getState().openDialog('exportProject'),
  },
  {
    id: 'file.exportPlugin',
    label: 'Export as Plugin…',
    enabled: hasProject,
    run: () => useLayout.getState().openDialog('buildPlugin'),
  },
  // -- Render ----------------------------------------------------------------------------------------
  // Every entry but the dialog renders immediately with the project's own settings; the dialog is where
  // scope and output options live. Each one navigates to the timeline first, because that is where the
  // progress bar and the finished file appear.
  {
    id: 'render.dialog',
    label: 'Render…',
    // Not Mod+R: the menu bar preventDefaults every match, and taking reload away from a web app is
    // hostile. Mod+M is what editors use for "export media" anyway.
    shortcut: 'Mod+M',
    enabled: hasTimeline,
    run: (ctx) => {
      ctx.navigate(`/p/${ctx.project!.id}/timeline`)
      useLayout.getState().openDialog('render')
    },
  },
  {
    id: 'render.timeline',
    label: 'Render Whole Timeline',
    enabled: hasTimeline,
    run: (ctx) => startRender(ctx, { scope: 'timeline' }, 'Rendering the timeline…'),
  },
  {
    id: 'render.clip',
    label: 'Render Selected Clip',
    enabled: (ctx) => hasProject(ctx) && selectedClip(ctx.project) !== null,
    run: (ctx) => {
      const selection = selectedClip(ctx.project)
      if (!selection) return ctx.toast('bad', 'Select a clip on the timeline first.')
      return startRender(
        ctx,
        { scope: 'clip', clip_id: selection.clip.id },
        `Rendering “${selection.clip.name || 'clip'}”…`,
      )
    },
  },
  {
    id: 'render.eachClip',
    label: 'Render Each Clip Separately',
    enabled: hasTimeline,
    run: (ctx) => startRender(ctx, { scope: 'clips' }, 'Rendering one file per clip…'),
  },
  {
    id: 'render.still',
    label: 'Render Still at Playhead',
    enabled: hasTimeline,
    run: (ctx) => startRender(ctx, { still: true }, 'Rendering a still…'),
  },
  // -- Templates -------------------------------------------------------------------------------------
  {
    id: 'shot.saveAsTemplate',
    label: 'Save Shot as Template…',
    enabled: (ctx) => ctx.shot !== null && ctx.shot.steps.length > 0,
    run: (ctx) => {
      const name = prompt('Template name', ctx.shot!.name)
      if (!name) return
      return guard(
        ctx,
        () => api.templates.saveShot(ctx.project!.id, ctx.shot!.id, { name }),
        `Saved “${name}” to the template library.`,
      ).then(() => ctx.queryClient.invalidateQueries({ queryKey: ['templates'] }))
    },
  },
  {
    id: 'shot.templateLibrary',
    label: 'Template Library…',
    enabled: hasProject,
    run: () => useLayout.getState().openDialog('templates'),
  },
  {
    id: 'file.close',
    label: 'Close Project',
    shortcut: 'Mod+W',
    enabled: hasProject,
    run: (ctx) => ctx.navigate('/projects'),
  },

  // -- Edit ------------------------------------------------------------------------------------------
  {
    id: 'edit.undo',
    label: 'Undo',
    shortcut: 'Mod+Z',
    enabled: (ctx) => hasProject(ctx) && ctx.history.undo > 0,
    run: (ctx) => guard(ctx, () => api.projects.undo(ctx.project!.id)),
  },
  {
    id: 'edit.redo',
    label: 'Redo',
    shortcut: 'Mod+Shift+Z',
    enabled: (ctx) => hasProject(ctx) && ctx.history.redo > 0,
    run: (ctx) => guard(ctx, () => api.projects.redo(ctx.project!.id)),
  },
  {
    id: 'edit.cut',
    label: 'Cut',
    shortcut: 'Mod+X',
    enabled: (ctx) => hasStep(ctx) || selectedClip(ctx.project) !== null,
    run: async (ctx) => {
      const clip = selectedClip(ctx.project)
      if (ctx.step) {
        useLayout.getState().setClipboard({ kind: 'step', payload: ctx.step, label: ctx.step.name })
        await guard(ctx, () => api.steps.remove(ctx.project!.id, ctx.step!.id), 'Step cut.')
        useStudio.getState().selectStep(null)
      } else if (clip) {
        useLayout.getState().setClipboard({ kind: 'clip', payload: clip.clip, label: clip.clip.name || 'clip' })
        await guard(
          ctx,
          () => api.timeline.removeClip(ctx.project!.id, clip.track, clip.clip.id),
          'Clip cut.',
        )
        useStudio.getState().selectClip(null)
      }
    },
  },
  {
    id: 'edit.copy',
    label: 'Copy',
    shortcut: 'Mod+C',
    enabled: (ctx) => hasStep(ctx) || selectedClip(ctx.project) !== null,
    run: (ctx) => {
      const clip = selectedClip(ctx.project)
      if (ctx.step) {
        useLayout.getState().setClipboard({ kind: 'step', payload: ctx.step, label: ctx.step.name })
        ctx.toast('info', `Copied step “${ctx.step.name}”.`)
      } else if (clip) {
        useLayout.getState().setClipboard({ kind: 'clip', payload: clip.clip, label: clip.clip.name || 'clip' })
        ctx.toast('info', 'Copied clip.')
      }
    },
  },
  {
    id: 'edit.paste',
    label: 'Paste',
    shortcut: 'Mod+V',
    enabled: (ctx) => useLayout.getState().clipboard !== null && hasProject(ctx),
    run: async (ctx) => {
      const clipboard = useLayout.getState().clipboard
      if (!clipboard || !ctx.project) return

      if (clipboard.kind === 'step') {
        if (!ctx.shot) return ctx.toast('bad', 'Open a shot to paste a step into.')
        const source = clipboard.payload as Step
        // One request, so the paste is a single undo step. The link topology is deliberately not
        // copied — pasting a step should not silently re-wire the graph.
        await guard(
          ctx,
          () =>
            api.steps.create(ctx.project!.id, ctx.shot!.id, source.workflow_id, {
              ui_pos: { x: source.ui_pos.x + 40, y: source.ui_pos.y + 40 },
              name: `${source.name} copy`,
              param_overrides: source.param_overrides,
              exposed_params: source.exposed_params,
              seed_mode: source.seed_mode,
            }),
          'Step pasted.',
        )
      } else {
        const track =
          ctx.project.timeline.tracks.find((t) => t.id === useStudio.getState().selectedClip?.trackId) ??
          ctx.project.timeline.tracks[0]
        if (!track) return ctx.toast('bad', 'Add a track to paste a clip into.')
        const source = clipboard.payload as Clip
        await guard(
          ctx,
          () =>
            api.timeline.createClip(ctx.project!.id, track.id, {
              source: source.source,
              duration: source.duration,
              name: source.name,
              text: source.text,
            }),
          'Clip pasted.',
        )
      }
    },
  },
  {
    id: 'edit.duplicateStep',
    label: 'Duplicate Step',
    shortcut: 'Mod+D',
    enabled: hasStep,
    run: async (ctx) => {
      useLayout.getState().setClipboard({ kind: 'step', payload: ctx.step!, label: ctx.step!.name })
      const paste = COMMANDS.find((c) => c.id === 'edit.paste')!
      await paste.run(ctx)
    },
  },
  {
    id: 'edit.delete',
    label: 'Delete',
    shortcut: 'Delete',
    danger: true,
    enabled: (ctx) => hasStep(ctx) || selectedClip(ctx.project) !== null,
    run: async (ctx) => {
      const clip = selectedClip(ctx.project)
      if (ctx.step) {
        await guard(ctx, () => api.steps.remove(ctx.project!.id, ctx.step!.id), 'Step deleted.')
        useStudio.getState().selectStep(null)
      } else if (clip) {
        await guard(
          ctx,
          () => api.timeline.removeClip(ctx.project!.id, clip.track, clip.clip.id),
          'Clip deleted.',
        )
        useStudio.getState().selectClip(null)
      }
    },
  },
  {
    id: 'edit.selectAll',
    label: 'Select All Steps',
    shortcut: 'Mod+A',
    enabled: () => useLayout.getState().canvas !== null,
    run: () => useLayout.getState().canvas?.selectAll(),
  },
  {
    id: 'edit.history',
    label: 'History…',
    shortcut: 'Mod+H',
    enabled: hasProject,
    run: () => {
      useStudio.getState().setHistoryTarget(null)
      useLayout.getState().openDialog('history')
    },
  },
  {
    id: 'edit.saveVersion',
    label: 'Save a Named Version…',
    enabled: hasProject,
    run: async (ctx) => {
      const label = prompt('Name this version', '')
      if (!label) return
      await guard(ctx, () => api.versions.tag(ctx.project!.id, label), `Saved version “${label}”.`)
    },
  },
  {
    id: 'edit.stepHistory',
    label: 'Show This Step’s History…',
    enabled: hasStep,
    run: (ctx) => {
      useStudio.getState().setHistoryTarget({
        scope: 'step', id: ctx.step!.id, name: ctx.step!.name,
      })
      useLayout.getState().openDialog('history')
    },
  },
  {
    id: 'edit.settings',
    label: 'Preferences…',
    shortcut: 'Mod+,',
    run: (ctx) => ctx.navigate('/settings'),
  },

  // -- View / Window ---------------------------------------------------------------------------------
  {
    id: 'window.toggleLeft',
    label: 'Show Workflows Panel',
    shortcut: 'Mod+1',
    checked: () => useLayout.getState().showLeftPanel,
    run: () => useLayout.getState().toggleLeftPanel(),
  },
  {
    id: 'window.toggleInspector',
    label: 'Show Inspector',
    shortcut: 'Mod+2',
    checked: () => useLayout.getState().showInspector,
    run: () => useLayout.getState().toggleInspector(),
  },
  {
    id: 'window.compact',
    label: 'Compact Mode',
    checked: () => useLayout.getState().compactMode,
    run: () => useLayout.getState().toggleCompact(),
  },
  {
    id: 'window.zoomIn',
    label: 'Zoom In',
    shortcut: 'Mod+=',
    enabled: () => useLayout.getState().canvas !== null,
    run: () => useLayout.getState().canvas?.zoomIn(),
  },
  {
    id: 'window.zoomOut',
    label: 'Zoom Out',
    shortcut: 'Mod+-',
    enabled: () => useLayout.getState().canvas !== null,
    run: () => useLayout.getState().canvas?.zoomOut(),
  },
  {
    id: 'window.fit',
    label: 'Fit Graph to Window',
    shortcut: 'Mod+0',
    enabled: () => useLayout.getState().canvas !== null,
    run: () => useLayout.getState().canvas?.fitView(),
  },
  {
    id: 'window.shots',
    label: 'Go to Shots',
    enabled: hasProject,
    run: (ctx) => ctx.navigate(`/p/${ctx.project!.id}/shots`),
  },
  {
    id: 'window.timeline',
    label: 'Go to Timeline',
    enabled: hasProject,
    run: (ctx) => ctx.navigate(`/p/${ctx.project!.id}/timeline`),
  },
  {
    id: 'window.settings',
    label: 'Go to Settings',
    run: (ctx) => ctx.navigate('/settings'),
  },
  {
    id: 'window.reset',
    label: 'Reset Layout',
    run: () => useLayout.getState().resetLayout(),
  },

  // -- Plugins ---------------------------------------------------------------------------------------
  {
    id: 'plugins.manage',
    label: 'Manage Plugins…',
    run: () => useLayout.getState().openDialog('plugins'),
  },
  {
    id: 'plugins.install',
    label: 'Load Plugin…',
    run: (ctx) =>
      pickFile('.cwsplugin', (file) =>
        guard(ctx, async () => {
          const plugin = await api.plugins.install(file)
          ctx.toast('ok', `Installed ${plugin.name} ${plugin.version}.`)
          ctx.queryClient.invalidateQueries({ queryKey: ['plugins'] })
        }),
      ),
  },
  {
    id: 'plugins.save',
    label: 'Save Project as Plugin…',
    enabled: hasProject,
    run: () => useLayout.getState().openDialog('buildPlugin'),
  },

  // -- Help ------------------------------------------------------------------------------------------
  {
    id: 'help.shortcuts',
    label: 'Keyboard Shortcuts',
    shortcut: 'Mod+/',
    run: () => useLayout.getState().openDialog('shortcuts'),
  },
  {
    id: 'help.docs',
    label: 'Documentation',
    run: () => window.open('https://github.com/magix/ComfyWebStudio#readme', '_blank', 'noopener'),
  },
  {
    id: 'help.comfy',
    label: 'Open ComfyUI',
    enabled: hasProject,
    run: async (ctx) => {
      try {
        const health = await api.health()
        const backend = health.backends?.[0]
        if (!backend) return ctx.toast('bad', 'No ComfyUI backend is configured.')
        window.open(backend.base_url, '_blank', 'noopener')
      } catch (error) {
        ctx.toast('bad', error instanceof ApiError ? error.message : String(error))
      }
    },
  },
  {
    id: 'help.about',
    label: 'About ComfyWebStudio',
    run: () => useLayout.getState().openDialog('about'),
  },
]

export const COMMANDS_BY_ID = new Map(COMMANDS.map((c) => [c.id, c]))

// -- menu structure ---------------------------------------------------------------------------------

export type MenuEntry =
  | { type: 'command'; id: string }
  | { type: 'separator' }
  | { type: 'submenu'; label: string; items: MenuEntry[] }

export interface Menu {
  id: string
  label: string
  items: MenuEntry[]
}

const cmd = (id: string): MenuEntry => ({ type: 'command', id })
const sep = (): MenuEntry => ({ type: 'separator' })

export const MENUS: Menu[] = [
  {
    id: 'file',
    label: 'File',
    items: [
      cmd('file.new'),
      cmd('file.open'),
      sep(),
      cmd('file.save'),
      cmd('file.duplicate'),
      sep(),
      {
        type: 'submenu',
        label: 'Import',
        items: [
          cmd('file.importWorkflowComfy'),
          cmd('file.importWorkflowFile'),
          cmd('file.importAsset'),
          sep(),
          cmd('file.importProject'),
        ],
      },
      {
        type: 'submenu',
        label: 'Export',
        items: [cmd('file.exportProject'), cmd('file.exportPlugin')],
      },
      sep(),
      cmd('shot.saveAsTemplate'),
      cmd('shot.templateLibrary'),
      sep(),
      cmd('render.dialog'),
      {
        type: 'submenu',
        label: 'Render',
        items: [
          cmd('render.timeline'),
          cmd('render.clip'),
          cmd('render.eachClip'),
          sep(),
          cmd('render.still'),
        ],
      },
      sep(),
      cmd('file.close'),
    ],
  },
  {
    id: 'edit',
    label: 'Edit',
    items: [
      cmd('edit.undo'),
      cmd('edit.redo'),
      sep(),
      cmd('edit.cut'),
      cmd('edit.copy'),
      cmd('edit.paste'),
      cmd('edit.duplicateStep'),
      sep(),
      cmd('edit.delete'),
      cmd('edit.selectAll'),
      sep(),
      cmd('edit.history'),
      cmd('edit.stepHistory'),
      cmd('edit.saveVersion'),
      sep(),
      cmd('edit.settings'),
    ],
  },
  {
    id: 'window',
    label: 'Window',
    items: [
      cmd('window.toggleLeft'),
      cmd('window.toggleInspector'),
      cmd('window.compact'),
      sep(),
      cmd('window.zoomIn'),
      cmd('window.zoomOut'),
      cmd('window.fit'),
      sep(),
      cmd('window.shots'),
      cmd('window.timeline'),
      cmd('window.settings'),
      sep(),
      cmd('window.reset'),
    ],
  },
  {
    id: 'plugins',
    label: 'Plugins',
    items: [cmd('plugins.manage'), sep(), cmd('plugins.install'), cmd('plugins.save')],
  },
  {
    id: 'help',
    label: 'Help',
    items: [
      cmd('help.shortcuts'),
      cmd('help.docs'),
      sep(),
      cmd('help.comfy'),
      sep(),
      cmd('help.about'),
    ],
  },
]

// -- shortcuts --------------------------------------------------------------------------------------

export const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

/** Renders `Mod+S` as `⌘S` or `Ctrl+S` depending on platform. */
export function formatShortcut(shortcut: string | undefined): string {
  if (!shortcut) return ''
  return shortcut
    .replace('Mod', IS_MAC ? '⌘' : 'Ctrl')
    .replace('Shift', IS_MAC ? '⇧' : 'Shift')
    .replace('Alt', IS_MAC ? '⌥' : 'Alt')
    .replace(/\+/g, IS_MAC ? '' : '+')
}

export function matchesShortcut(event: KeyboardEvent, shortcut: string): boolean {
  const parts = shortcut.split('+')
  const key = parts[parts.length - 1].toLowerCase()
  const wantMod = parts.includes('Mod')
  const wantShift = parts.includes('Shift')
  const wantAlt = parts.includes('Alt')

  const mod = IS_MAC ? event.metaKey : event.ctrlKey
  if (wantMod !== mod) return false
  if (wantShift !== event.shiftKey) return false
  if (wantAlt !== event.altKey) return false

  const pressed = event.key.toLowerCase()
  // `=` and `-` double as zoom keys; accept the shifted `+` too.
  if (key === '=' && (pressed === '=' || pressed === '+')) return true
  return pressed === key
}

// -- helpers ----------------------------------------------------------------------------------------

function pickFile(accept: string, onPick: (file: File) => void) {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = accept
  input.onchange = () => {
    const file = input.files?.[0]
    if (file) onPick(file)
    input.remove()
  }
  input.click()
}

export { download }
