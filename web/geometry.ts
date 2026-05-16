export type DisplaySize = { width: number; height: number }

export function nativePoint(clientX: number, clientY: number, rect: Pick<DOMRect, 'left' | 'top' | 'width' | 'height'>, size: DisplaySize) {
  return {
    x: Math.max(0, Math.min(size.width - 1, Math.floor((clientX - rect.left) * size.width / rect.width))),
    y: Math.max(0, Math.min(size.height - 1, Math.floor((clientY - rect.top) * size.height / rect.height))),
  }
}
