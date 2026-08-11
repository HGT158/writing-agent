export function utf16ToCodePointOffset(text: string, utf16Offset: number): number {
  const clamped = Math.max(0, Math.min(utf16Offset, text.length))
  return Array.from(text.slice(0, clamped)).length
}

export function codePointToUtf16Offset(text: string, codePointOffset: number): number {
  const points = Array.from(text)
  const clamped = Math.max(0, Math.min(codePointOffset, points.length))
  return points.slice(0, clamped).join('').length
}

