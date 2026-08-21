/**
 * CodeMirror 语法高亮主题化（phase7 P1-2，架构 §5.10 v1.22 契约补齐）。
 *
 * basicSetup 内置的 defaultHighlightStyle 用内联样式 spec 生成匿名类名（ͼN），
 * DOM 上不会出现语义类，styles.css 为五套主题定义的 --cm-* 变量因此永不命中。
 * 这里用 tagHighlighter 定义 tag → cm-* 语义类映射（对齐官方 classHighlighter
 * 的 tok-* 方案，但沿用本项目既有的 cm-* 前缀与 CSS 规则），使 token 颜色由
 * 当前主题的语义变量驱动，深色主题完整覆盖语法高亮。
 */
import { syntaxHighlighting } from '@codemirror/language'
import { tagHighlighter, tags } from '@lezer/highlight'

/** Markdown 常见 token 加少量代码内嵌场景，类名与 styles.css 既有规则一致。 */
const semanticHighlightSpecs = [
  { tag: tags.link, class: 'cm-link' },
  { tag: tags.url, class: 'cm-url' },
  { tag: tags.heading, class: 'cm-heading' },
  { tag: tags.emphasis, class: 'cm-emphasis' },
  { tag: tags.strong, class: 'cm-strong' },
  { tag: tags.keyword, class: 'cm-keyword' },
  { tag: [tags.atom, tags.bool], class: 'cm-atom' },
  { tag: tags.number, class: 'cm-number' },
  { tag: [tags.string, tags.special(tags.string)], class: 'cm-string' },
  { tag: [tags.regexp, tags.escape], class: 'cm-string2' },
  { tag: tags.comment, class: 'cm-comment' },
  { tag: tags.meta, class: 'cm-meta' },
  { tag: tags.variableName, class: 'cm-variableName' },
  { tag: tags.propertyName, class: 'cm-propertyName' },
  { tag: tags.typeName, class: 'cm-typeName' },
  { tag: tags.operator, class: 'cm-operator' },
  { tag: tags.punctuation, class: 'cm-punctuation' },
  { tag: tags.bracket, class: 'cm-bracket' },
  { tag: tags.processingInstruction, class: 'cm-meta' },
]

export const THEME_SYNTAX_CLASSES = [
  ...new Set(semanticHighlightSpecs.map((spec) => spec.class)),
]

const semanticClassHighlighter = tagHighlighter(semanticHighlightSpecs)

/**
 * 注册语义类高亮；置于 basicSetup 之后。注意不能带 `fallback: true`——
 * 那会与 basicSetup 内置的 defaultHighlightStyle 同为 fallback 高亮器，
 * 两者合并后匿名内联样式类仍然生效，语义类不会出现。
 */
export const themeSyntaxHighlighting = syntaxHighlighting(semanticClassHighlighter)
