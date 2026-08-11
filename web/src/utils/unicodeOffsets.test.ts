import { describe, expect, it } from 'vitest'

import { codePointToUtf16Offset, utf16ToCodePointOffset } from './unicodeOffsets'

describe('Unicode selection offsets', () => {
  it('converts CodeMirror UTF-16 offsets to API code-point offsets', () => {
    const text = 'A😀中文'

    expect(utf16ToCodePointOffset(text, 0)).toBe(0)
    expect(utf16ToCodePointOffset(text, 1)).toBe(1)
    expect(utf16ToCodePointOffset(text, 3)).toBe(2)
    expect(utf16ToCodePointOffset(text, 5)).toBe(4)
  })

  it('converts API code-point offsets back to CodeMirror UTF-16 offsets', () => {
    const text = 'A😀中文'

    expect(codePointToUtf16Offset(text, 0)).toBe(0)
    expect(codePointToUtf16Offset(text, 2)).toBe(3)
    expect(codePointToUtf16Offset(text, 4)).toBe(5)
  })
})
