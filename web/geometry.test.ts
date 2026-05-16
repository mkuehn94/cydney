import { describe, expect, it } from 'vitest'
import { nativePoint } from './geometry'

const landscape = { width: 320, height: 240 }
describe('preview input uses native CYD pixels', () => {
  it('maps native-size taps directly', () => {
    expect(nativePoint(120, 224, { left: 0, top: 0, width: 320, height: 240 }, landscape)).toEqual({ x: 120, y: 224 })
  })
  it('removes both preview scaling and viewport offset', () => {
    expect(nativePoint(340, 498, { left: 100, top: 50, width: 640, height: 480 }, landscape)).toEqual({ x: 120, y: 224 })
  })
  it('handles portrait panels at three times magnification', () => {
    expect(nativePoint(360, 480, { left: 0, top: 0, width: 720, height: 960 }, { width: 240, height: 320 })).toEqual({ x: 120, y: 160 })
  })
  it('clamps release positions outside panel bounds', () => {
    expect(nativePoint(1000, -10, { left: 0, top: 0, width: 320, height: 240 }, landscape)).toEqual({ x: 319, y: 0 })
  })
  it('uses the last valid pixel at the bottom-right edge', () => {
    expect(nativePoint(320, 240, { left: 0, top: 0, width: 320, height: 240 }, landscape)).toEqual({ x: 319, y: 239 })
  })
})
