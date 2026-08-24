let cachedText = ''
let cachedOffsets: number[] = [0]

function utf16Offsets(text: string): number[] {
  if (text === cachedText) return cachedOffsets
  const offsets = [0]
  let utf16 = 0
  for (const point of text) {
    utf16 += point.length
    offsets.push(utf16)
  }
  cachedText = text
  cachedOffsets = offsets
  return offsets
}

export function utf16ToCodePointOffset(text: string, utf16Offset: number): number {
  const clamped = Math.max(0, Math.min(utf16Offset, text.length))
  const offsets = utf16Offsets(text)
  let low = 0
  let high = offsets.length
  while (low < high) {
    const middle = (low + high) >>> 1
    if (offsets[middle] <= clamped) low = middle + 1
    else high = middle
  }
  return low - 1
}

export function codePointToUtf16Offset(text: string, codePointOffset: number): number {
  const offsets = utf16Offsets(text)
  const clamped = Math.max(0, Math.min(codePointOffset, offsets.length - 1))
  return offsets[clamped]
}
