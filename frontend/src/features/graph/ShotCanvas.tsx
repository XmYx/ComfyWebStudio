import { useCallback, useMemo } from 'react'
import {
  Background, BackgroundVariant, Controls, ReactFlow, ReactFlowProvider,
  type Connection, type Edge, type Node, type NodeChange,
} from '@xyflow/react'

import { api, ApiError } from '@/api/client'
import type { Project, Shot } from '@/api/types'
import { KIND_COLOR, canConnect } from '@/lib/kinds'
import { useStudio } from '@/store/studio'
import { Empty, useToast } from '@/components/ui'
import { StepNode, type StepNodeData } from './StepNode'

const NODE_TYPES = { step: StepNode }

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
  const liveSteps = useStudio((s) => s.liveSteps)
  const selectedStepId = useStudio((s) => s.selectedStepId)
  const selectStep = useStudio((s) => s.selectStep)

  const nodes = useMemo<Node<StepNodeData>[]>(
    () =>
      shot.steps.map((step) => {
        const live = liveSteps[step.id]
        const preview = live?.outputs?.find((a) => a.thumb)
        return {
          id: step.id,
          type: 'step',
          position: { x: step.ui_pos.x, y: step.ui_pos.y },
          data: {
            step,
            workflow: project.workflows[step.workflow_id],
            live,
            thumbUrl: preview?.thumb ? api.media.url(project.id, preview.thumb) : null,
            selected: step.id === selectedStepId,
            onRun: onRunStep,
            onToggle: async (stepId: string) => {
              const target = shot.steps.find((s) => s.id === stepId)
              if (!target) return
              await api.steps.update(project.id, stepId, { enabled: !target.enabled })
              onChanged()
            },
          },
        }
      }),
    [shot.steps, project, liveSteps, selectedStepId, onRunStep, onChanged],
  )

  const edges = useMemo<Edge[]>(
    () =>
      shot.links.map((link) => {
        const source = shot.steps.find((s) => s.id === link.from_step)
        const workflow = source ? project.workflows[source.workflow_id] : undefined
        const port = workflow?.ports.find((p) => p.key === link.from_port && p.direction === 'out')
        return {
          id: link.id,
          source: link.from_step,
          sourceHandle: link.from_port,
          target: link.to_step,
          targetHandle: link.to_port,
          animated: liveSteps[link.from_step]?.status === 'running',
          style: { stroke: port ? KIND_COLOR[port.kind] : undefined, strokeWidth: 2 },
        }
      }),
    [shot.links, shot.steps, project.workflows, liveSteps],
  )

  const portOf = useCallback(
    (stepId: string | null, portKey: string | null | undefined, direction: 'in' | 'out') => {
      const step = shot.steps.find((s) => s.id === stepId)
      const workflow = step ? project.workflows[step.workflow_id] : undefined
      return workflow?.ports.find((p) => p.key === portKey && p.direction === direction)
    },
    [shot.steps, project.workflows],
  )

  const isValidConnection = useCallback(
    (connection: Connection | Edge) => {
      if (connection.source === connection.target) return false
      const from = portOf(connection.source, connection.sourceHandle, 'out')
      const to = portOf(connection.target, connection.targetHandle, 'in')
      if (!from || !to) return false
      return canConnect(from.kind, to.kind)
    },
    [portOf],
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

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      for (const change of changes) {
        if (change.type === 'position' && change.dragging === false && change.position) {
          // Persist only on drag end: saving every intermediate position would hammer the API.
          void api.steps.update(project.id, change.id, {
            ui_pos: { x: change.position.x, y: change.position.y },
          })
        }
        if (change.type === 'select' && change.selected) {
          selectStep(change.id)
        }
      }
    },
    [project.id, selectStep],
  )

  const onNodesDelete = useCallback(
    async (deleted: Node[]) => {
      for (const node of deleted) {
        try {
          await api.steps.remove(project.id, node.id)
        } catch (error) {
          toast.push('bad', (error as ApiError).message)
        }
      }
      selectStep(null)
      onChanged()
    },
    [project.id, onChanged, selectStep, toast],
  )

  if (!shot.steps.length) {
    return (
      <Empty title="This shot has no steps yet">
        Add a workflow from the left panel. Each step runs one ComfyUI workflow; connect an output port to
        an input port to chain them.
      </Empty>
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
      onNodeClick={(_e, node) => selectStep(node.id)}
      fitView
      fitViewOptions={{ padding: 0.25, maxZoom: 1 }}
      proOptions={{ hideAttribution: true }}
      deleteKeyCode={['Backspace', 'Delete']}
      minZoom={0.2}
      maxZoom={1.8}
    >
      <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="#232a35" />
      <Controls showInteractive={false} />
    </ReactFlow>
  )
}
