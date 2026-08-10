import { describe, expect, it } from 'vitest'

import {
  allGroups, dockAt, dockAtEdge, ensurePlaced, group, groupOf, placedWidgets, removeWidget,
  resizeSplit, split, visibleTree, type DockNode,
} from './dockTree'
import type { WidgetId } from './layout'

/** A readable shape for assertions: splits as `row[...]` / `col[...]`, groups as their tab list. */
function shape(node: DockNode | null): string {
  if (!node) return '∅'
  if (node.type === 'group') return `(${node.tabs.join(',')})`
  return `${node.direction === 'row' ? 'row' : 'col'}[${node.children.map(shape).join(' ')}]`
}

const base = () =>
  split('row', [group(['shots', 'workflows']), group(['canvas']), group(['inspector'])], [0.2, 0.55, 0.25])

describe('docking onto a group', () => {
  it('tabs a widget in when dropped on the middle', () => {
    const target = allGroups(base())[1].id
    const tree = base()
    const moved = dockAt(tree, 'shots', allGroups(tree)[1].id, 'centre')

    expect(shape(moved)).toBe('row[(workflows) (canvas,shots) (inspector)]')
    expect(target).toBeTruthy()
  })

  it('splits the group horizontally when dropped on its left or right', () => {
    const tree = base()
    const canvas = allGroups(tree)[1].id
    expect(shape(dockAt(tree, 'shots', canvas, 'right')))
      .toBe('row[(workflows) (canvas) (shots) (inspector)]')
  })

  it('splits the group vertically when dropped above or below — the point of the tree', () => {
    const tree = base()
    const canvas = allGroups(tree)[1].id
    expect(shape(dockAt(tree, 'inspector', canvas, 'bottom')))
      .toBe('row[(shots,workflows) col[(canvas) (inspector)]]')
  })

  it('puts the panel first when dropped on the top edge', () => {
    const tree = base()
    const canvas = allGroups(tree)[1].id
    const moved = dockAt(tree, 'inspector', canvas, 'top')
    expect(shape(moved)).toBe('row[(shots,workflows) col[(inspector) (canvas)]]')
  })

  it('collapses the group a widget left behind when it was the last tab', () => {
    const tree = base()
    const shotsGroup = allGroups(tree)[0].id
    // canvas leaves its own group entirely, so that group must disappear rather than linger empty.
    expect(shape(dockAt(tree, 'canvas', shotsGroup, 'centre')))
      .toBe('row[(shots,workflows,canvas) (inspector)]')
  })

  it('is a no-op when a lone tab is dropped back into its own group', () => {
    const tree = base()
    const canvas = allGroups(tree)[1].id
    expect(shape(dockAt(tree, 'canvas', canvas, 'centre'))).toBe(shape(tree))
  })

  it('keeps the widget when it is the group it is splitting off', () => {
    const tree = base()
    const canvas = allGroups(tree)[1].id
    const moved = dockAt(tree, 'canvas', canvas, 'right')
    expect(placedWidgets(moved)).toContain('canvas')
  })

  it('flattens a nested split of the same direction', () => {
    const tree = base()
    const canvas = allGroups(tree)[1].id
    const once = dockAt(tree, 'shots', canvas, 'right')
    const twice = dockAt(once, 'workflows', groupOf(once, 'shots')!.id, 'right')
    // Four columns, not a column containing a column.
    expect(shape(twice)).toBe('row[(canvas) (shots) (workflows) (inspector)]')
  })
})

describe('docking to a workspace edge', () => {
  it('wraps the whole layout so the panel spans it', () => {
    expect(shape(dockAtEdge(base(), 'inspector', 'bottom')))
      .toBe('col[row[(shots,workflows) (canvas)] (inspector)]')
  })

  it('spans the top when asked for the top', () => {
    expect(shape(dockAtEdge(base(), 'inspector', 'top')))
      .toBe('col[(inspector) row[(shots,workflows) (canvas)]]')
  })
})

describe('removing and restoring', () => {
  it('drops a widget out of the tree and collapses what it emptied', () => {
    expect(shape(removeWidget(base(), 'canvas'))).toBe('row[(shots,workflows) (inspector)]')
  })

  it('leaves a usable tree even when everything is removed', () => {
    let tree: DockNode = base()
    for (const id of ['shots', 'workflows', 'canvas', 'inspector'] as WidgetId[]) {
      tree = removeWidget(tree, id)
    }
    expect(allGroups(tree)).toHaveLength(1)
  })

  it('puts an unplaced widget back somewhere reachable', () => {
    const without = removeWidget(base(), 'canvas')
    expect(placedWidgets(ensurePlaced(without, 'canvas'))).toContain('canvas')
  })

  it('leaves an already-placed widget where it is', () => {
    const tree = base()
    expect(ensurePlaced(tree, 'canvas')).toBe(tree)
  })
})

describe('sizing', () => {
  it('moves one boundary and leaves the other children alone', () => {
    const tree = base()
    const resized = resizeSplit(tree, tree.id, 0, 0.3)
    expect(resized.type).toBe('split')
    if (resized.type !== 'split') return
    expect(resized.sizes[0]).toBeCloseTo(0.3)
    expect(resized.sizes[0] + resized.sizes[1]).toBeCloseTo(0.75)
    expect(resized.sizes[2]).toBeCloseTo(0.25)
  })

  it('refuses to squeeze a panel out of existence', () => {
    const tree = base()
    const resized = resizeSplit(tree, tree.id, 0, 0)
    if (resized.type !== 'split') throw new Error('expected a split')
    expect(resized.sizes[0]).toBeGreaterThan(0)
  })
})

describe('hiding', () => {
  const hide = (...hidden: WidgetId[]) => (id: WidgetId) => !hidden.includes(id)

  it('collapses a group whose every tab is hidden, without forgetting them', () => {
    const tree = base()
    expect(shape(visibleTree(tree, hide('inspector')))).toBe('row[(shots,workflows) (canvas)]')
    // The real tree is untouched, which is how showing it again puts it back where it was.
    expect(placedWidgets(tree)).toContain('inspector')
  })

  it('keeps the tabs that are still visible', () => {
    expect(shape(visibleTree(base(), hide('shots')))).toBe('row[(workflows) (canvas) (inspector)]')
  })

  it('picks a visible tab when the active one is hidden', () => {
    const visible = visibleTree(base(), hide('shots'))
    expect(groupOf(visible!, 'workflows')?.active).toBe('workflows')
  })

  it('returns nothing when the whole workspace is hidden', () => {
    expect(visibleTree(base(), () => false)).toBeNull()
  })
})
