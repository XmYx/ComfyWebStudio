import { useCallback, useEffect, useMemo } from 'react'
import {
  applyNodeChanges,
  Background, BackgroundVariant, Controls, ReactFlow, ReactFlowProvider, useNodesState, useReactFlow,
  type Connection, type Edge, type Node, type NodeChange,
} from '@xyflow/react'

import { api, ApiError } from '@/api/client'
import type { PortKind, Project, Shot, ValueNodeKind } from '@/api/types'
import { VALUE_PORT } from '@/api/types'
import { KIND_COLOR, canConnect } from '@/lib/kinds'
import { useStudio } from '@/store/studio'
import { useLayout } from '@/store/layout'
import { Empty, useToast } from '@/components/ui'
import { ContextMenu, useContextMenu, type MenuItem } from '@/components/ContextMenu'
import { useCommandContext } from '@/features/menu/useCommandContext'
import { StepNode, type StepNodeData } from './StepNode'
import { ValueNodeCard, VALUE_NODE_LABELS, type ValueNodeData } from './ValueNodeCard'

const NODE_TYPES = { step: StepNode, value: ValueNodeCard }

/** Offered by the "Add value node" menu, in the order they appear there. */
const VALUE_NODE_KINDS: ValueNodeKind[] = ['string', 'int', 'float', 'boolean', 'media']

/** Shared so a step with no links does not get a fresh Set on every render, remounting its node. */
const EMPTY_KEYS: Set<string> = new Set()

type CanvasNodeData = StepNodeData | ValueNodeData

/** What a media value node offers. Mirrors `ValueNode.output_kind` in core/models.py. */
function valueNodeKind(
  project: Project, kind: ValueNodeKind, assetId: string | null, mediaKind: PortKind,
): PortKind {
  if (kind !== 'media') return kind
  return (assetId && project.assets[assetId]?.kind) || mediaKind
}

/**
 * The kind carried by the producing end of a link — a step's output port, or a value node.
 *
 * Both are sources on this canvas, so everything that needs to colour an edge or judge a connection has
 * to look in both places.
 */
function sourceKind(
  project: Project, shot: Shot, nodeId: string | null, portKey: string | null | undefined,
): PortKind | undefined {
  const step = shot.steps.find((s) => s.id === nodeId)
  if (step) {
    const workflow = project.workflows[step.workflow_id]
    return workflow?.ports.find((p) => p.key === portKey && p.direction === 'out')?.kind
  }
  const node = shot.nodes.find((n) => n.id === nodeId)
  if (node && portKey === VALUE_PORT) {
    return valueNodeKind(project, node.kind, node.asset_id, node.media_kind)
  }
  return undefined
}

interface Props {
  project: Project
  shot: Shot
  onChanged: () => void
  onRunStep: (stepId: string) => void
}

/**
 * The shot canvas: steps as nodes, ports as handles, links drawn between them.
 *
 * Connections are validated locally as the user drags (so an impossible link simply will not attach) and
 * again on the server when it is created — the server's answer is authoritative and carries the reason.
 */
export function ShotCanvas(props: Props) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  )
}

function Canvas({ project, shot, onChanged, onRunStep }: Props) {
  const toast = useToast()
  const flow = useReactFlow()
  const setCanvas = useLayout((s) => s.setCanvas)
  const liveSteps = useStudio((s) => s.liveSteps)
  const selectedStepId = useStudio((s) => s.selectedStepId)
  const selectStep = useStudio((s) => s.selectStep)

  const assets = useMemo(() => Object.values(project.assets), [project.assets])

  // Value nodes share the canvas with steps but are a different shape: they carry a constant and never
  // run, so they have their own node type and their own endpoints.
  const derivedValueNodes = useMemo<Node<ValueNodeData>[]>(
    () =>
      shot.nodes.map((node) => ({
        id: node.id,
        type: 'value',
        position: { x: node.ui_pos.x, y: node.ui_pos.y },
        ...(node.ui_size?.w > 0 && node.ui_size?.h > 0
          ? { width: node.ui_size.w, height: node.ui_size.h }
          : {}),
        data: {
          node,
          projectId: project.id,
          assets,
          outputKind: valueNodeKind(project, node.kind, node.asset_id, node.media_kind),
          onChanged,
        },
      })),
    [shot.nodes, project, assets, onChanged],
  )

  // React Flow is used in controlled mode, so a drag only moves a node if the change is applied back to
  // the nodes we hand it. `derived` is the truth from the server; `nodes` is what the canvas edits.
  // Input ports fed by a link, per step: a parameter pinned to a node has to show as driven from
  // upstream rather than as something the user can type into.
  const linkedByStep = useMemo(() => {
    const map = new Map<string, Set<string>>()
    for (const link of shot.links) {
      const keys = map.get(link.to_step) ?? new Set<string>()
      keys.add(link.to_port)
      map.set(link.to_step, keys)
    }
    return map
  }, [shot.links])

  const derivedSteps = useMemo<Node<StepNodeData>[]>(
    () =>
      shot.steps.map((step) => {
        const live = liveSteps[step.id]
        const preview = live?.outputs?.find((a) => a.thumb)
        return {
          id: step.id,
          type: 'step',
          position: { x: step.ui_pos.x, y: step.ui_pos.y },
          ...(step.ui_size?.w > 0 && step.ui_size?.h > 0
            ? { width: step.ui_size.w, height: step.ui_size.h }
            : {}),
          data: {
            step,
            workflow: project.workflows[step.workflow_id],
            live,
            thumbUrl: preview?.thumb ? api.media.url(project.id, preview.thumb) : null,
            selected: step.id === selectedStepId,
            linkedKeys: linkedByStep.get(step.id) ?? EMPTY_KEYS,
            onRun: onRunStep,
            onToggle: async (stepId: string) => {
              const target = shot.steps.find((s) => s.id === stepId)
              if (!target) return
              await api.steps.update(project.id, stepId, { enabled: !target.enabled })
              onChanged()
            },
            onParamChange: async (stepId: string, key: string, value: unknown) => {
              // PATCH merges, so sending only the edited key leaves everything else alone.
              await api.steps.update(project.id, stepId, { param_overrides: { [key]: value } })
              onChanged()
            },
          },
        }
      }),
    [shot.steps, project, liveSteps, selectedStepId, linkedByStep, onRunStep, onChanged],
  )

  const derived = useMemo<Node<CanvasNodeData>[]>(
    () => [...derivedSteps, ...derivedValueNodes],
    [derivedSteps, derivedValueNodes],
  )

  const [nodes, setNodes] = useNodesState<Node<CanvasNodeData>>(derived)

  // Fold server updates (progress, new previews, added or removed steps) into the canvas without
  // discarding canvas-owned state. Position and selection live here, not on the server between saves —
  // rebuilding them from `derived` would cancel a drag in progress and clear the selection on every
  // event tick, which also hides the resize handles.
  useEffect(() => {
    setNodes((current) => {
      const existing = new Map(current.map((node) => [node.id, node]))
      return derived.map((node) => {
        const previous = existing.get(node.id)
        return previous
          ? { ...node, position: previous.position, selected: previous.selected }
          : node
      })
    })
  }, [derived, setNodes])

  const edges = useMemo<Edge[]>(
    () =>
      shot.links.map((link) => {
        const kind = sourceKind(project, shot, link.from_step, link.from_port)
        return {
          id: link.id,
          source: link.from_step,
          sourceHandle: link.from_port,
          target: link.to_step,
          targetHandle: link.to_port,
          animated: liveSteps[link.from_step]?.status === 'running',
          style: { stroke: kind ? KIND_COLOR[kind] : undefined, strokeWidth: 2 },
        }
      }),
    [shot, project, liveSteps],
  )

  // Hand the Window menu a handle on this canvas, and take it back on unmount so a stale one cannot
  // outlive the view it belonged to.
  useEffect(() => {
    setCanvas({
      zoomIn: () => flow.zoomIn({ duration: 150 }),
      zoomOut: () => flow.zoomOut({ duration: 150 }),
      fitView: () => flow.fitView({ padding: 0.25, duration: 200 }),
      selectAll: () => setNodes((current) => current.map((n) => ({ ...n, selected: true }))),
    })
    return () => setCanvas(null)
  }, [flow, setCanvas, setNodes])

  const inputKindOf = useCallback(
    (stepId: string | null, portKey: string | null | undefined) => {
      const step = shot.steps.find((s) => s.id === stepId)
      const workflow = step ? project.workflows[step.workflow_id] : undefined
      return workflow?.ports.find((p) => p.key === portKey && p.direction === 'in')?.kind
    },
    [shot.steps, project.workflows],
  )

  const isValidConnection = useCallback(
    (connection: Connection | Edge) => {
      if (connection.source === connection.target) return false
      const from = sourceKind(project, shot, connection.source, connection.sourceHandle)
      const to = inputKindOf(connection.target, connection.targetHandle)
      if (!from || !to) return false
      return canConnect(from, to)
    },
    [project, shot, inputKindOf],
  )

  const onConnect = useCallback(
    async (connection: Connection) => {
      if (!connection.source || !connection.target) return
      try {
        await api.links.create(project.id, shot.id, {
          from_step: connection.source,
          from_port: connection.sourceHandle ?? '',
          to_step: connection.target,
          to_port: connection.targetHandle ?? '',
        })
        onChanged()
      } catch (error) {
        toast.push('bad', (error as ApiError).message)
      }
    },
    [project.id, shot.id, onChanged, toast],
  )

  const onEdgesDelete = useCallback(
    async (deleted: Edge[]) => {
      for (const edge of deleted) {
        try {
          await api.links.remove(project.id, shot.id, edge.id)
        } catch (error) {
          toast.push('bad', (error as ApiError).message)
        }
      }
      onChanged()
    },
    [project.id, shot.id, onChanged, toast],
  )

  // Steps and value nodes live at different endpoints, so every canvas gesture has to know which it just
  // moved, resized or deleted.
  const isValueNode = useCallback(
    (nodeId: string) => shot.nodes.some((n) => n.id === nodeId),
    [shot.nodes],
  )

  const onNodesChange = useCallback(
    (changes: NodeChange<Node<CanvasNodeData>>[]) => {
      // Apply first — without this the node snaps back and dragging does nothing.
      setNodes((current) => applyNodeChanges(changes, current))

      for (const change of changes) {
        const endpoint = 'id' in change && isValueNode(change.id) ? api.nodes : api.steps

        if (change.type === 'position' && change.dragging === false && change.position) {
          // Persist only on drag end: saving every intermediate position would hammer the API.
          void endpoint.update(project.id, change.id, {
            ui_pos: { x: change.position.x, y: change.position.y },
          })
        }
        // React Flow reports resizing as dimension changes; `resizing === false` is the release.
        if (change.type === 'dimensions' && change.resizing === false && change.dimensions) {
          void endpoint
            .update(project.id, change.id, {
              ui_size: { w: change.dimensions.width, h: change.dimensions.height },
            })
            .then(onChanged)
        }
        if (change.type === 'select' && change.selected) {
          // Only steps have an inspector; selecting a value node clears it rather than leaving the
          // previous step's parameters on screen next to a node they have nothing to do with.
          selectStep(isValueNode(change.id) ? null : change.id)
        }
      }
    },
    [project.id, selectStep, setNodes, onChanged, isValueNode],
  )

  const onNodesDelete = useCallback(
    async (deleted: Node[]) => {
      for (const node of deleted) {
        try {
          if (isValueNode(node.id)) await api.nodes.remove(project.id, node.id)
          else await api.steps.remove(project.id, node.id)
        } catch (error) {
          toast.push('bad', (error as ApiError).message)
        }
      }
      selectStep(null)
      onChanged()
    },
    [project.id, onChanged, selectStep, toast, isValueNode],
  )

  const commandContext = useCommandContext()
  const contextMenu = useContextMenu()

  const addStepItems = useCallback(
    (): MenuItem[] =>
      Object.values(project.workflows).map((workflow) => ({
        type: 'action' as const,
        label: workflow.name,
        onSelect: async () => {
          try {
            await api.steps.create(project.id, shot.id, workflow.id)
            onChanged()
          } catch (error) {
            toast.push('bad', (error as ApiError).message)
          }
        },
      })),
    [project, shot.id, onChanged, toast],
  )

  /** Drops the node where the user right-clicked, rather than in a corner they then have to find. */
  const addValueNodeItems = useCallback(
    (event: React.MouseEvent): MenuItem[] =>
      VALUE_NODE_KINDS.map((kind) => ({
        type: 'action' as const,
        label: VALUE_NODE_LABELS[kind],
        onSelect: async () => {
          try {
            const at = flow.screenToFlowPosition({ x: event.clientX, y: event.clientY })
            await api.nodes.create(project.id, shot.id, { kind, ui_pos: at })
            onChanged()
          } catch (error) {
            toast.push('bad', (error as ApiError).message)
          }
        },
      })),
    [project.id, shot.id, flow, onChanged, toast],
  )

  const paneMenu = (event: React.MouseEvent): MenuItem[] => [
    { type: 'header', label: shot.name },
    {
      type: 'submenu',
      label: 'Add step',
      items: addStepItems(),
      disabled: !Object.keys(project.workflows).length,
    },
    { type: 'submenu', label: 'Add value node', items: addValueNodeItems(event) },
    { type: 'command', id: 'file.importWorkflowComfy' },
    { type: 'separator' },
    { type: 'command', id: 'edit.paste' },
    { type: 'command', id: 'edit.selectAll' },
    { type: 'separator' },
    {
      type: 'action',
      label: 'Fit graph to window',
      shortcut: 'Mod+0',
      onSelect: () => flow.fitView({ padding: 0.25, duration: 200 }),
    },
    {
      type: 'action',
      label: 'Arrange steps in run order',
      onSelect: () => void autoLayout(event),
    },
  ]

  const nodeMenu = (step: (typeof shot.steps)[number]): MenuItem[] => [
    { type: 'header', label: step.name },
    { type: 'action', label: 'Run this step', shortcut: undefined, onSelect: () => onRunStep(step.id) },
    { type: 'separator' },
    { type: 'command', id: 'edit.copy' },
    { type: 'command', id: 'edit.cut' },
    { type: 'command', id: 'edit.duplicateStep' },
    { type: 'separator' },
    {
      type: 'action',
      label: step.enabled ? 'Disable step' : 'Enable step',
      checked: step.enabled,
      onSelect: async () => {
        await api.steps.update(project.id, step.id, { enabled: !step.enabled })
        onChanged()
      },
    },
    {
      type: 'action',
      label: 'Rename…',
      onSelect: async () => {
        const name = prompt('Step name', step.name)
        if (name && name !== step.name) {
          await api.steps.update(project.id, step.id, { name })
          onChanged()
        }
      },
    },
    {
      type: 'action',
      label: 'Reset size',
      disabled: !(step.ui_size?.w > 0),
      onSelect: async () => {
        await api.steps.update(project.id, step.id, { ui_size: { w: 0, h: 0 } })
        onChanged()
      },
    },
    { type: 'separator' },
    {
      type: 'action',
      label: 'Open workflow in ComfyUI',
      onSelect: async () => {
        try {
          const result = await api.workflows.openInComfy(project.id, step.workflow_id)
          if (result.hint) toast.push('bad', result.hint)
          window.open(result.url, '_blank', 'noopener')
        } catch (error) {
          toast.push('bad', (error as ApiError).message)
        }
      },
    },
    { type: 'separator' },
    { type: 'command', id: 'edit.delete' },
  ]

  const valueNodeMenu = (node: (typeof shot.nodes)[number]): MenuItem[] => [
    { type: 'header', label: node.name || VALUE_NODE_LABELS[node.kind] },
    {
      type: 'action',
      label: 'Rename…',
      onSelect: async () => {
        const name = prompt('Node name', node.name)
        if (name !== null && name !== node.name) {
          await api.nodes.update(project.id, node.id, { name })
          onChanged()
        }
      },
    },
    {
      type: 'action',
      label: 'Reset size',
      disabled: !(node.ui_size?.w > 0),
      onSelect: async () => {
        await api.nodes.update(project.id, node.id, { ui_size: { w: 0, h: 0 } })
        onChanged()
      },
    },
    { type: 'separator' },
    {
      type: 'action',
      label: 'Delete node',
      danger: true,
      onSelect: async () => {
        try {
          await api.nodes.remove(project.id, node.id)
          onChanged()
        } catch (error) {
          toast.push('bad', (error as ApiError).message)
        }
      },
    },
  ]

  const edgeMenu = (edgeId: string): MenuItem[] => {
    const link = shot.links.find((l) => l.id === edgeId)
    return [
      { type: 'header', label: link ? `${link.from_port} → ${link.to_port}` : 'Link' },
      {
        type: 'action',
        label: 'Disconnect',
        danger: true,
        onSelect: async () => {
          try {
            await api.links.remove(project.id, shot.id, edgeId)
            onChanged()
          } catch (error) {
            toast.push('bad', (error as ApiError).message)
          }
        },
      },
    ]
  }

  /** Lay the steps out left to right in dependency order — the shape most shots want anyway. */
  const autoLayout = async (_event: React.MouseEvent) => {
    try {
      const report = await api.shots.validate(project.id, shot.id)
      const order = report.order.length ? report.order : shot.steps.map((s) => s.id)
      await Promise.all(
        order.map((stepId, index) =>
          api.steps.update(project.id, stepId, { ui_pos: { x: 40 + index * 320, y: 80 } }),
        ),
      )
      onChanged()
      setTimeout(() => flow.fitView({ padding: 0.25, duration: 200 }), 100)
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    }
  }

  if (!shot.steps.length && !shot.nodes.length) {
    return (
      <div
        className="h-full"
        onContextMenu={(event) => contextMenu.open(event, paneMenu(event))}
      >
        <Empty title="This shot has no steps yet">
          Add a workflow from the left panel, or right-click here. Each step runs one ComfyUI workflow;
          connect an output port to an input port to chain them. Right-click also adds value nodes —
          text, numbers or imported media — to feed those inputs.
        </Empty>
        <ContextMenu state={contextMenu.menu} onClose={contextMenu.close} context={commandContext} />
      </div>
    )
  }

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      onConnect={onConnect}
      onEdgesDelete={onEdgesDelete}
      onNodesChange={onNodesChange}
      onNodesDelete={onNodesDelete}
      isValidConnection={isValidConnection}
      onPaneClick={() => selectStep(null)}
      onNodeClick={(_e, node) => selectStep(isValueNode(node.id) ? null : node.id)}
      onPaneContextMenu={(event) => contextMenu.open(event as React.MouseEvent, paneMenu(event as React.MouseEvent))}
      onNodeContextMenu={(event, node) => {
        const value = shot.nodes.find((n) => n.id === node.id)
        if (value) {
          selectStep(null)
          contextMenu.open(event, valueNodeMenu(value))
          return
        }
        const step = shot.steps.find((s) => s.id === node.id)
        selectStep(node.id)
        if (step) contextMenu.open(event, nodeMenu(step))
      }}
      onEdgeContextMenu={(event, edge) => contextMenu.open(event, edgeMenu(edge.id))}
      fitView
      fitViewOptions={{ padding: 0.25, maxZoom: 1 }}
      proOptions={{ hideAttribution: true }}
      deleteKeyCode={['Backspace', 'Delete']}
      minZoom={0.2}
      maxZoom={1.8}
    >
      <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="#232a35" />
      <Controls showInteractive={false} />
      <ContextMenu state={contextMenu.menu} onClose={contextMenu.close} context={commandContext} />
    </ReactFlow>
  )
}
