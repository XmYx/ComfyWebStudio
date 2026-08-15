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

/**
 * One shot's run, as the shots panel needs to see it.
 *
 * Kept per shot rather than folded into `activeRun` because a queue has several: one rendering, several
 * waiting, several already finished. A single "current run" cannot describe that, and describing it
 * wrongly is worse than not showing it.
 */
export interface ShotRun {
  runId: string
  stepIds: string[]
  status: Run['status']
  error?: string | null
}

/** A queue of shots being rendered one after another. */
export interface ShotQueueState {
  id: string
  /** In render order. */
  shotIds: string[]
  /** Shots that have reached a terminal state, and what they reached. */
  done: Record<string, Run['status']>
  /** The shot rendering right now. */
  current: string | null
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
  /**
   * What the embedded ComfyUI panel is showing.
   *
   * Held here rather than inside the panel so that opening a workflow can point the panel at it from
   * anywhere — the workflow library, a node's menu — without those knowing the panel exists. Null means
   * "just show ComfyUI", which is what it opens with.
   */
  comfyUrl: string | null
  /** Bumped on every request, so asking for the URL already showing still reloads the frame. */
  comfyNonce: number
  selectedClip: { trackId: string; clipId: string } | null
  /**
   * Every clip selected on the timeline, in the order they were picked.
   *
   * `selectedClip` stays as the *primary* one — the inspector edits a single clip, and "the clip the
   * inspector is showing" and "the clips an operation covers" are different questions. It is always the
   * last entry here, so the two can never disagree about whether anything is selected at all.
   */
  selectedClips: Array<{ trackId: string; clipId: string }>

  activeRun: Run | null
  liveSteps: Record<string, LiveStep>
  /** Every shot's most recent run, keyed by shot id. */
  shotRuns: Record<string, ShotRun>
  /** The queue rendering several shots, if one is going. */
  queue: ShotQueueState | null
  /**
   * Shots ticked in the panel, for rendering a few at once.
   *
   * Separate from `shotId`: which shot the canvas is showing and which shots the next render covers are
   * different questions, and making one answer both means you cannot look at a shot without adding it to
   * the queue.
   */
  selectedShotIds: string[]
  /** Timeline transport. Shared, so a floating monitor and the timeline agree on where we are. */
  playhead: number
  playing: boolean
  renderProgress: { id: string; progress: number; message: string } | null
  historyTarget: HistoryTarget | null

  setProject: (id: string | null) => void
  setShot: (id: string | null) => void
  selectStep: (id: string | null) => void
  selectInstance: (id: string | null) => void
  openInstance: (id: string | null) => void
  /** Point the embedded ComfyUI panel somewhere. Bumped even for the same URL, to force a reload. */
  showInComfy: (url: string | null) => void
  selectClip: (selection: { trackId: string; clipId: string } | null) => void
  /** Add to, remove from, or extend the timeline selection — what shift and ctrl clicking do. */
  selectClips: (selection: Array<{ trackId: string; clipId: string }>) => void
  toggleClip: (selection: { trackId: string; clipId: string }) => void

  beginRun: (run: Run) => void
  endRun: (status: Run['status'], error?: string | null, shotId?: string | null) => void
  /** Replace the ticked set outright — the panel works out what a click means. */
  setSelectedShots: (ids: string[]) => void
  /** Record a run this page never watched, from what the server says about it. */
  adoptShotRun: (
    shotId: string, runId: string, status: Run['status'], error?: string | null,
  ) => void
  beginQueue: (queue: ShotQueueState) => void
  patchQueue: (shotId: string, status: Run['status']) => void
  endQueue: () => void
  patchStep: (stepId: string, patch: LiveStep) => void
  seedFromResults: (results: Record<string, { step_run: StepRun }>) => void
  setPlayhead: (time: number) => void
  setPlaying: (playing: boolean) => void
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
  comfyUrl: null,
  comfyNonce: 0,
  selectedClip: null,
  selectedClips: [],
  activeRun: null,
  liveSteps: {},
  shotRuns: {},
  queue: null,
  selectedShotIds: [],
  playhead: 0,
  playing: false,
  renderProgress: null,
  historyTarget: null,

  setProject: (id) =>
    set({
      projectId: id, shotId: null, selectedStepId: null, selectedInstanceId: null,
      openInstanceId: null, liveSteps: {}, activeRun: null, shotRuns: {}, queue: null,
      selectedShotIds: [],
    }),
  setShot: (id) =>
    set({ shotId: id, selectedStepId: null, selectedInstanceId: null, openInstanceId: null }),
  // Selecting one clears the other: the inspector shows a step or a placed template, never both.
  selectStep: (id) => set({ selectedStepId: id, selectedInstanceId: null }),
  selectInstance: (id) => set({ selectedInstanceId: id, selectedStepId: null }),
  openInstance: (id) => set({ openInstanceId: id, selectedStepId: null, selectedInstanceId: null }),
  showInComfy: (url) => set((s) => ({ comfyUrl: url, comfyNonce: s.comfyNonce + 1 })),
  selectClip: (selection) =>
    set({ selectedClip: selection, selectedClips: selection ? [selection] : [] }),

  selectClips: (selection) =>
    set({ selectedClips: selection, selectedClip: selection.at(-1) ?? null }),

  toggleClip: (selection) =>
    set((state) => {
      const without = state.selectedClips.filter((c) => c.clipId !== selection.clipId)
      const next =
        without.length === state.selectedClips.length ? [...without, selection] : without
      return { selectedClips: next, selectedClip: next.at(-1) ?? null }
    }),
  setPlayhead: (playhead) => set({ playhead }),
  setPlaying: (playing) => set({ playing }),

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
      shotRuns: run.shot_id
        ? {
            ...state.shotRuns,
            [run.shot_id]: {
              runId: run.id,
              stepIds: run.step_runs.map((sr) => sr.step_id),
              status: 'running',
              error: null,
            },
          }
        : state.shotRuns,
    })),

  endRun: (status, error, shotId) =>
    set((state) => {
      // Which shot ended is what the event says, not which one happens to be selected: in a queue the
      // run that just finished is rarely the one the user is looking at.
      const ended = shotId ?? state.activeRun?.shot_id ?? null
      if (!ended) {
        return { activeRun: state.activeRun ? { ...state.activeRun, status, error: error ?? null } : null }
      }
      // Recorded even when this page never saw the run start — a tab opened halfway through a queue
      // still has to be able to say how the shot in front of it ended.
      const before = state.shotRuns[ended] ?? { runId: '', stepIds: [] }
      return {
        activeRun: state.activeRun ? { ...state.activeRun, status, error: error ?? null } : null,
        shotRuns: { ...state.shotRuns, [ended]: { ...before, status, error: error ?? null } },
      }
    }),

  setSelectedShots: (selectedShotIds) => set({ selectedShotIds }),

  adoptShotRun: (shotId, runId, status, error) =>
    set((state) => ({
      // Never over an entry this page watched itself: that one has the step ids, which is what the
      // progress bar needs and what the server's summary cannot give back.
      shotRuns: state.shotRuns[shotId]?.stepIds.length
        ? state.shotRuns
        : { ...state.shotRuns, [shotId]: { runId, stepIds: [], status, error: error ?? null } },
    })),

  beginQueue: (queue) => set({ queue }),

  patchQueue: (shotId, status) =>
    set((state) => {
      if (!state.queue) return {}
      const running = status === 'running'
      return {
        queue: {
          ...state.queue,
          current: running ? shotId : state.queue.current === shotId ? null : state.queue.current,
          done: running ? state.queue.done : { ...state.queue.done, [shotId]: status },
        },
      }
    }),

  endQueue: () => set({ queue: null }),

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
  clearLive: () => set({ liveSteps: {}, activeRun: null, shotRuns: {}, queue: null }),
}))
