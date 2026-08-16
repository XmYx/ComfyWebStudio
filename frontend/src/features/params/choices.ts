/**
 * What a combo picker should offer.
 *
 * Its own module with a test because getting it wrong is silent. A `select` handed a value none of its
 * options match renders the *first* option instead — so a checkpoint that is merely not installed on this
 * machine reads as a completely different one that is, and the next edit anywhere on the step writes that
 * back over the value the workflow actually had.
 */

export interface Choice {
  value: string
  label: string
  /** True for a value the workflow holds that this ComfyUI does not offer. */
  missing: boolean
}

/** The options to render, with the current value carried along when nothing offered matches it. */
export function choiceOptions(current: string, choices: readonly string[]): Choice[] {
  const offered = choices.map((value) => ({ value, label: value, missing: false }))
  if (!current || choices.includes(current)) return offered
  return [{ value: current, label: `${current} — not on this ComfyUI`, missing: true }, ...offered]
}

/** Whether the picker is showing something it cannot actually run. */
export function isMissing(current: string, choices: readonly string[]): boolean {
  return Boolean(current) && !choices.includes(current)
}
