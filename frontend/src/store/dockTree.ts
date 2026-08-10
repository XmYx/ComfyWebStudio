/**
 * The workspace layout, as a tree.
 *
 * A leaf is a **group**: a tab strip plus whichever tab is in front. A branch is a **split**: children laid
 * out in a row or a column, with a fraction of the space each. That is the whole model, and it is what lets
 * a panel be docked *beside* or *above* another one instead of only tabbed into the same box — docking to
 * an edge of a group replaces that group with a split containing both.
 *
 * Two rules keep it well-formed, applied by `prune` after every move:
 *
 *  * a split with one child is pointless — it becomes that child;
 *  * a group with no tabs at all is dead space — it goes, unless it is the last one, since the workspace
 *    always needs somewhere to drop things.
 *
 * Note that *hidden* and *floating* widgets stay in the tree. Only their rendering is suppressed, so
 * showing a panel again puts it back exactly where it was rather than guessing.
 */

import type { WidgetId } from './layout'

export type SplitDirection = 'row' | 'column'

/** Where a drop lands relative to the group under the pointer. */
export type DropPosition = 'centre' | 'left' | 'right' | 'top' | 'bottom'

export interface DockGroupNode {
  type: 'group'
  id: string
  tabs: WidgetId[]
  active: WidgetId | null
}

export interface DockSplitNode {
  type: 'split'
  id: string
  direction: SplitDirection
  children: DockNode[]
  /** One fraction per child, summing to 1. */
  sizes: number[]
}

export type DockNode = DockGroupNode | DockSplitNode

/** How much of the space a panel takes when it is docked alongside an existing one. */
const NEW_SPLIT_FRACTION = 0.32

/** A split cannot squeeze a child below this, or a panel becomes impossible to grab again. */
export const MIN_SPLIT_FRACTION = 0.08

let counter = 0
const nextId = (prefix: string) => `${prefix}_${Date.now().toString(36)}_${(counter++).toString(36)}`

export const group = (tabs: WidgetId[], active?: WidgetId | null): DockGroupNode => ({
  type: 'group',
  id: nextId('g'),
  tabs,
  active: active ?? tabs[0] ?? null,
})

export const split = (
  direction: SplitDirection, children: DockNode[], sizes?: number[],
): DockSplitNode => ({
  type: 'split',
  id: nextId('s'),
  direction,
  children,
  sizes: sizes ?? children.map(() => 1 / Math.max(1, children.length)),
})

// -- reading ---------------------------------------------------------------------------------------

export function findGroup(node: DockNode, groupId: string): DockGroupNode | null {
  if (node.type === 'group') return node.id === groupId ? node : null
  for (const child of node.children) {
    const found = findGroup(child, groupId)
    if (found) return found
  }
  return null
}

export function groupOf(node: DockNode, widget: WidgetId): DockGroupNode | null {
  if (node.type === 'group') return node.tabs.includes(widget) ? node : null
  for (const child of node.children) {
    const found = groupOf(child, widget)
    if (found) return found
  }
  return null
}

export function allGroups(node: DockNode): DockGroupNode[] {
  return node.type === 'group' ? [node] : node.children.flatMap(allGroups)
}

/** Every widget the tree places, in layout order. */
export function placedWidgets(node: DockNode): WidgetId[] {
  return allGroups(node).flatMap((g) => g.tabs)
}

// -- editing ---------------------------------------------------------------------------------------

/** Deep copy, so a store update never mutates the previous state in place. */
export function cloneNode(node: DockNode): DockNode {
  return node.type === 'group'
    ? { ...node, tabs: [...node.tabs] }
    : { ...node, children: node.children.map(cloneNode), sizes: [...node.sizes] }
}

/** Drop a widget from wherever it currently sits. Does not prune. */
function detach(node: DockNode, widget: WidgetId): void {
  if (node.type === 'group') {
    const index = node.tabs.indexOf(widget)
    if (index >= 0) {
      node.tabs.splice(index, 1)
      if (node.active === widget) node.active = node.tabs[0] ?? null
    }
    return
  }
  node.children.forEach((child) => detach(child, widget))
}

/**
 * Collapse whatever the last edit left behind.
 *
 * Returns null when the subtree has nothing left in it, which is how an emptied branch removes itself from
 * its parent rather than lingering as a zero-width column.
 */
function prune(node: DockNode): DockNode | null {
  if (node.type === 'group') return node.tabs.length ? node : null

  const kept: DockNode[] = []
  const sizes: number[] = []
  node.children.forEach((child, index) => {
    const pruned = prune(child)
    if (!pruned) return
    // A split of the same direction is the same layout with extra nesting — flattening keeps the tree
    // shallow, which matters because every level is another set of splitters to drag.
    if (pruned.type === 'split' && pruned.direction === node.direction) {
      const share = node.sizes[index] ?? 1 / node.children.length
      pruned.children.forEach((grandchild, inner) => {
        kept.push(grandchild)
        sizes.push(share * (pruned.sizes[inner] ?? 1 / pruned.children.length))
      })
      return
    }
    kept.push(pruned)
    sizes.push(node.sizes[index] ?? 1 / node.children.length)
  })

  if (!kept.length) return null
  if (kept.length === 1) return kept[0]
  return { ...node, children: kept, sizes: normalize(sizes) }
}

export function normalize(sizes: number[]): number[] {
  const total = sizes.reduce((sum, size) => sum + size, 0)
  if (!(total > 0)) return sizes.map(() => 1 / Math.max(1, sizes.length))
  return sizes.map((size) => size / total)
}

/** Replace one node with another, by id. */
function replace(node: DockNode, targetId: string, replacement: DockNode): DockNode {
  if (node.id === targetId) return replacement
  if (node.type === 'split') {
    node.children = node.children.map((child) => replace(child, targetId, replacement))
  }
  return node
}

/**
 * Dock a widget relative to a group.
 *
 * `centre` tabs it into that group; the four edges split the group in two and put the widget on that side,
 * which is the whole point of the tree — panels beside, above and below each other, not only stacked.
 */
export function dockAt(
  root: DockNode, widget: WidgetId, targetGroupId: string, position: DropPosition,
): DockNode {
  let tree = cloneNode(root)
  const target = findGroup(tree, targetGroupId)
  if (!target) return tree

  // Docking a group's only tab onto itself would delete the target out from under us; nothing to do.
  if (target.tabs.length === 1 && target.tabs[0] === widget) {
    if (position === 'centre') return tree
  }

  detach(tree, widget)

  // detach may have emptied the target, in which case the widget simply becomes its content again.
  const stillThere = findGroup(tree, targetGroupId)
  if (!stillThere || !stillThere.tabs.length) {
    const fallback = stillThere ?? target
    fallback.tabs = [widget]
    fallback.active = widget
    return prune(tree) ?? group([widget])
  }

  if (position === 'centre') {
    stillThere.tabs.push(widget)
    stillThere.active = widget
    return prune(tree) ?? group([widget])
  }

  const direction: SplitDirection = position === 'left' || position === 'right' ? 'row' : 'column'
  const before = position === 'left' || position === 'top'
  const incoming = group([widget])
  const children = before ? [incoming, stillThere] : [stillThere, incoming]
  const sizes = before
    ? [NEW_SPLIT_FRACTION, 1 - NEW_SPLIT_FRACTION]
    : [1 - NEW_SPLIT_FRACTION, NEW_SPLIT_FRACTION]

  tree = replace(tree, stillThere.id, split(direction, children, sizes))
  return prune(tree) ?? group([widget])
}

/**
 * Dock a widget against an outer edge of the whole workspace, spanning it.
 *
 * Distinct from docking to the edge of the outermost group: that would only span the height of whatever
 * that group covers, so there would be no way to get a full-width strip back once the layout has columns.
 */
export function dockAtEdge(root: DockNode, widget: WidgetId, edge: Exclude<DropPosition, 'centre'>): DockNode {
  const tree = cloneNode(root)
  detach(tree, widget)
  const rest = prune(tree)
  const incoming = group([widget])
  if (!rest) return incoming

  const direction: SplitDirection = edge === 'left' || edge === 'right' ? 'row' : 'column'
  const before = edge === 'left' || edge === 'top'
  return split(
    direction,
    before ? [incoming, rest] : [rest, incoming],
    before ? [NEW_SPLIT_FRACTION, 1 - NEW_SPLIT_FRACTION] : [1 - NEW_SPLIT_FRACTION, NEW_SPLIT_FRACTION],
  )
}

/** Take a widget out of the tree entirely — what floating it does. */
export function removeWidget(root: DockNode, widget: WidgetId): DockNode {
  const tree = cloneNode(root)
  detach(tree, widget)
  return prune(tree) ?? group([])
}

/** Bring a tab to the front of its group. */
export function activateTab(root: DockNode, groupId: string, widget: WidgetId): DockNode {
  const tree = cloneNode(root)
  const target = findGroup(tree, groupId)
  if (target && target.tabs.includes(widget)) target.active = widget
  return tree
}

/** Put a widget back in the tree if it is not in it — used when a floating panel is re-docked. */
export function ensurePlaced(root: DockNode, widget: WidgetId): DockNode {
  if (groupOf(root, widget)) return root
  const tree = cloneNode(root)
  const groups = allGroups(tree)
  // The largest group is the least disruptive home for a panel with nowhere to go.
  const home = groups.sort((a, b) => b.tabs.length - a.tabs.length)[0]
  if (!home) return group([widget])
  home.tabs.push(widget)
  home.active = widget
  return tree
}

/** Resize one boundary of a split, clamped so neither side can be squeezed out of existence. */
export function resizeSplit(
  root: DockNode, splitId: string, index: number, fraction: number,
): DockNode {
  const tree = cloneNode(root)
  const apply = (node: DockNode): void => {
    if (node.type !== 'split') return
    if (node.id === splitId) {
      const pair = node.sizes[index] + node.sizes[index + 1]
      const clamped = Math.max(MIN_SPLIT_FRACTION, Math.min(pair - MIN_SPLIT_FRACTION, fraction))
      node.sizes = [...node.sizes]
      node.sizes[index] = clamped
      node.sizes[index + 1] = pair - clamped
      return
    }
    node.children.forEach(apply)
  }
  apply(tree)
  return tree
}

/**
 * The tree with everything invisible removed, for rendering.
 *
 * Hidden and floating widgets keep their place in the real tree so they can come back to it, but they must
 * not hold space on screen — a column containing only hidden panels has to collapse.
 */
export function visibleTree(node: DockNode, isVisible: (id: WidgetId) => boolean): DockNode | null {
  if (node.type === 'group') {
    const tabs = node.tabs.filter(isVisible)
    if (!tabs.length) return null
    const active = node.active && tabs.includes(node.active) ? node.active : tabs[0]
    return { ...node, tabs, active }
  }

  const kept: DockNode[] = []
  const sizes: number[] = []
  node.children.forEach((child, index) => {
    const visible = visibleTree(child, isVisible)
    if (!visible) return
    kept.push(visible)
    sizes.push(node.sizes[index] ?? 1 / node.children.length)
  })

  if (!kept.length) return null
  if (kept.length === 1) return kept[0]
  return { ...node, children: kept, sizes: normalize(sizes) }
}
