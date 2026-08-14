import { StateEffect, StateField } from '@codemirror/state'
import { Decoration, EditorView, WidgetType, type DecorationSet } from '@codemirror/view'

/**
 * 待确认 change set 的编辑器内联视图（架构 §5.10）：原文标为删除态，
 * 建议文本以新增态紧随其后，并附内联接受/放弃控件。
 * 装饰只读展示，正文仍然只在 apply 成功后由服务端返回内容同步回编辑器。
 */
export interface InlineDiff {
  changeSetId: string
  from: number
  to: number
  replacement: string
  busy: boolean
}

export interface InlineDiffHandlers {
  accept: (changeSetId: string) => void
  reject: (changeSetId: string) => void
}

export const setInlineDiffs = StateEffect.define<InlineDiff[]>()

class ProposalWidget extends WidgetType {
  constructor(
    private readonly diff: InlineDiff,
    private readonly handlers: InlineDiffHandlers,
  ) {
    super()
  }

  eq(other: ProposalWidget) {
    return other.diff.changeSetId === this.diff.changeSetId
      && other.diff.replacement === this.diff.replacement
      && other.diff.busy === this.diff.busy
  }

  toDOM() {
    const host = document.createElement('span')
    host.className = 'cm-diff-proposal'
    host.setAttribute('data-change-set-id', this.diff.changeSetId)

    const body = document.createElement('span')
    body.className = 'cm-diff-inserted'
    body.textContent = this.diff.replacement
    host.appendChild(body)

    const actions = document.createElement('span')
    actions.className = 'cm-diff-inline-actions'
    actions.appendChild(this.button('接受', 'cm-diff-accept', () => this.handlers.accept(this.diff.changeSetId)))
    actions.appendChild(this.button('放弃', 'cm-diff-reject', () => this.handlers.reject(this.diff.changeSetId)))
    host.appendChild(actions)
    return host
  }

  private button(label: string, className: string, action: () => void) {
    const button = document.createElement('button')
    button.type = 'button'
    button.className = className
    button.textContent = label
    button.disabled = this.diff.busy
    button.addEventListener('mousedown', (event) => event.preventDefault())
    button.addEventListener('click', (event) => {
      event.preventDefault()
      event.stopPropagation()
      if (!this.diff.busy) action()
    })
    return button
  }

  ignoreEvent() {
    return true
  }
}

const removedMark = Decoration.mark({ class: 'cm-diff-removed' })

function buildDecorations(
  diffs: InlineDiff[],
  handlers: InlineDiffHandlers,
  docLength: number,
): DecorationSet {
  const ranges = []
  for (const diff of diffs) {
    const from = Math.max(0, Math.min(diff.from, docLength))
    const to = Math.max(from, Math.min(diff.to, docLength))
    if (to > from) ranges.push(removedMark.range(from, to))
    ranges.push(
      Decoration.widget({ widget: new ProposalWidget(diff, handlers), side: 1 }).range(to),
    )
  }
  return Decoration.set(ranges, true)
}

export function inlineDiffField(handlers: InlineDiffHandlers) {
  return StateField.define<DecorationSet>({
    create: () => Decoration.none,
    update(value, transaction) {
      let next = value.map(transaction.changes)
      for (const effect of transaction.effects) {
        if (!effect.is(setInlineDiffs)) continue
        next = buildDecorations(effect.value, handlers, transaction.state.doc.length)
      }
      return next
    },
    provide: (field) => EditorView.decorations.from(field),
  })
}
