import { StateEffect, StateField } from '@codemirror/state'
import { Decoration, EditorView, type DecorationSet } from '@codemirror/view'

/**
 * CodeMirror 的原生选区在编辑器失焦后不可见，用户在选区工具栏里输入提示词时
 * 会看不到改写目标。这个装饰在工具栏打开期间保持选区高亮（架构 §5.10）。
 */
export const setFrozenSelection = StateEffect.define<{ from: number; to: number } | null>()

const frozenMark = Decoration.mark({ class: 'cm-frozen-selection' })

export const frozenSelectionField = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(value, transaction) {
    let next = value.map(transaction.changes)
    for (const effect of transaction.effects) {
      if (!effect.is(setFrozenSelection)) continue
      const range = effect.value
      const limit = transaction.state.doc.length
      const from = range ? Math.max(0, Math.min(range.from, limit)) : 0
      const to = range ? Math.max(from, Math.min(range.to, limit)) : 0
      next = to > from ? Decoration.set([frozenMark.range(from, to)]) : Decoration.none
    }
    return next
  },
  provide: (field) => EditorView.decorations.from(field),
})
