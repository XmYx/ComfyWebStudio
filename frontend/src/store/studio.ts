/**
 * Client state: what the user is looking at, and what every step's latest result is.
 *
 * Server data lives in TanStack Query. This store holds selection plus the live run state that arrives
 * over the websocket — keeping run progress here rather than in the query cache means a step's progress
 * bar updates without invalidating and refetching the whole project on every tick.
 */

import { create } from 'zustand'
import type { Artifact, Run, StepRun } from '@/api/types'

export interface LiveStep {
  status: StepRun['status']
  progress: number
  error?: string | null
  outputs?: Artifact[]
  cached?: boolean
}

/** What the History panel is scoped to. `null` means the whole project. */
export interface HistoryTarget {
  scope: 'project' | 'shot' | 'step' | 'workflow' | 'timeline' | 'track'
  id: string
  name: string
}

interface StudioState {
  projectId: string | null
  shotId: string | null
  selectedStepId: string | null
  /** A placed template selected on the canvas. Mutually exclusive with `selectedStepId`. */
  selectedInstanceId: string | null
  /** The template currently drilled into, so the canvas can show what is inside a placed node. */
  openInstanceId: string | null
  selectedClip: { trackId: string; clipId: string } | null

  activeRun: Run | null
  liveSteps: Record<string, LiveStep>
  renderProgress: { id: string; progress: number; message: string } | null
  historyTarget: HistoryTarget | null

  setProject: (id: string | null) => void
  setShot: (id: string | null) => void
  selectStep: (id: string | null) => void
  selectInstance: (id: string | null) => void
  openInstance: (id: string | null) => void
  selectClip: (selection: { trackId: string; clipId: string } | null) => void

  beginRun: (run: Run) => void
  endRun: (status: Run['status'], error?: string | null) => void
  patchStep: (stepId: string, patch: LiveStep) => void
  seedFromResults: (results: Record<string, { step_run: StepRun }>) => void
  setRenderProgress: (progress: StudioState['renderProgress']) => void
  setHistoryTarget: (target: HistoryTarget | null) => void
  clearLive: () => void
}

export const useStudio = create<StudioState>((set) => ({
  projectId: null,
  shotId: null,
  selectedStepId: null,
  selectedInstanceId: null,
  openInstanceId: null,
  selectedClip: null,
  activeRun: null,
  liveSteps: {},
  renderProgress: null,
  historyTarget: null,

  setProject: (id) =>
    set({
      projectId: id, shotId: null, selectedStepId: null, selectedInstanceId: null,
      openInstanceId: null, liveSteps: {}, activeRun: null,
    }),
  setShot: (id) =>
    set({ shotId: id, selectedStepId: null, selectedInstanceId: null, openInstanceId: null }),
  // Selecting one clears the other: the inspector shows a step or a placed template, never both.
  selectStep: (id) => set({ selectedStepId: id, selectedInstanceId: null }),
  selectInstance: (id) => set({ selectedInstanceId: id, selectedStepId: null }),
  openInstance: (id) => set({ openInstanceId: id, selectedStepId: null, selectedInstanceId: null }),
  selectClip: (selection) => set({ selectedClip: selection }),

  beginRun: (run) =>
    set((state) => ({
      activeRun: run,
      // Queued steps show as pending immediately; anything already finished keeps its previous result
      // so previews do not blank out while a new run starts.
      liveSteps: run.step_runs.reduce<Record<string, LiveStep>>(
        (acc, sr) => {
          acc[sr.step_id] = { status: 'pending', progress: 0, outputs: state.liveSteps[sr.step_id]?.outputs }
          return acc
        },
        { ...state.liveSteps },
      ),
    })),

  endRun: (status, error) =>
    set((state) => ({
      activeRun: state.activeRun ? { ...state.activeRun, status, error: error ?? null } : null,
    })),

  patchStep: (stepId, patch) =>
    set((state) => ({
      liveSteps: { ...state.liveSteps, [stepId]: { ...state.liveSteps[stepId], ...patch } },
    })),

  seedFromResults: (results) =>
    set(() => ({
      liveSteps: Object.fromEntries(
        Object.entries(results).map(([stepId, entry]) => [
          stepId,
          {
            status: entry.step_run.status,
            progress: 1,
            outputs: entry.step_run.outputs,
            cached: entry.step_run.cached,
          },
        ]),
      ),
    })),

  setRenderProgress: (progress) => set({ renderProgress: progress }),
  setHistoryTarget: (historyTarget) => set({ historyTarget }),
  clearLive: () => set({ liveSteps: {}, activeRun: null }),
}))
