/**
 * AS.7.0 — `lib/auth-visual/nebula-shader.ts` contract tests.
 *
 * Three families:
 *
 *   1. GLSL source pinning — fragment shader has the AS.7.0 marker
 *      pair and stays inside the ~50-line budget; uniforms named
 *      exactly as the React leaf expects.
 *   2. Pure helper truth tables — `clampStarLayers` /
 *      `clampGravity` boundaries.
 *   3. WebGL setup contract — `setupNebulaProgram` returns null on
 *      compile / link failure (so the leaf can degrade to the
 *      static gradient) and a populated handle on success.
 */

import { describe, expect, it, vi } from "vitest"

import {
  NEBULA_FRAGMENT_MAIN_BEGIN,
  NEBULA_FRAGMENT_MAIN_END,
  NEBULA_FRAGMENT_SHADER,
  NEBULA_VERTEX_SHADER,
  cleanupNebulaProgram,
  clampGravity,
  clampStarLayers,
  drawNebulaFrame,
  nebulaFragmentMainLineCount,
  setupNebulaProgram,
} from "@/lib/auth-visual/nebula-shader"

describe("AS.7.0 nebula-shader — GLSL source pinning", () => {
  it("vertex shader exposes `a_position` and writes `v_uv`", () => {
    expect(NEBULA_VERTEX_SHADER).toContain("attribute vec2 a_position")
    expect(NEBULA_VERTEX_SHADER).toContain("varying vec2 v_uv")
  })

  it("fragment shader declares all five uniforms the React leaf supplies", () => {
    expect(NEBULA_FRAGMENT_SHADER).toContain("uniform float u_time")
    expect(NEBULA_FRAGMENT_SHADER).toContain("uniform vec2  u_resolution")
    expect(NEBULA_FRAGMENT_SHADER).toContain("uniform vec2  u_mouse")
    expect(NEBULA_FRAGMENT_SHADER).toContain("uniform int   u_starLayers")
    expect(NEBULA_FRAGMENT_SHADER).toContain("uniform float u_gravity")
  })

  it("fragment shader brackets `main` with the AS.7.0 markers", () => {
    expect(NEBULA_FRAGMENT_SHADER).toContain(NEBULA_FRAGMENT_MAIN_BEGIN)
    expect(NEBULA_FRAGMENT_SHADER).toContain(NEBULA_FRAGMENT_MAIN_END)
    expect(NEBULA_FRAGMENT_SHADER.indexOf(NEBULA_FRAGMENT_MAIN_BEGIN)).toBeLessThan(
      NEBULA_FRAGMENT_SHADER.indexOf(NEBULA_FRAGMENT_MAIN_END),
    )
  })

  it("main body stays within the ~50 GLSL-line budget", () => {
    const lines = nebulaFragmentMainLineCount()
    expect(lines).toBeGreaterThan(20)
    // Generous upper bound — AS.7.0 spec asks for "~50 行 GLSL"; 60
    // gives room for minor tweaks without making this drift guard
    // chronically fragile.
    expect(lines).toBeLessThanOrEqual(60)
  })

  it("fragment shader reads three independent star layers conditionally", () => {
    expect(NEBULA_FRAGMENT_SHADER).toContain("u_starLayers >= 1")
    expect(NEBULA_FRAGMENT_SHADER).toContain("u_starLayers >= 2")
    expect(NEBULA_FRAGMENT_SHADER).toContain("u_starLayers >= 3")
  })

  it("fragment shader applies the cursor gravity well", () => {
    expect(NEBULA_FRAGMENT_SHADER).toContain("u_gravity")
    // Gravity is exponentially attenuated by distance to the mouse.
    expect(NEBULA_FRAGMENT_SHADER).toMatch(/exp\(-md \* /)
  })
})

describe("AS.7.0 nebula-shader — pure clamps", () => {
  it("clampStarLayers handles non-finite + negative + over-budget", () => {
    expect(clampStarLayers(NaN)).toBe(0)
    expect(clampStarLayers(-1)).toBe(0)
    expect(clampStarLayers(0)).toBe(0)
    expect(clampStarLayers(1)).toBe(1)
    expect(clampStarLayers(2.4)).toBe(2)
    expect(clampStarLayers(2.6)).toBe(3)
    expect(clampStarLayers(3)).toBe(3)
    expect(clampStarLayers(99)).toBe(3)
  })

  it("clampGravity handles non-finite + negative + over-budget", () => {
    expect(clampGravity(NaN)).toBe(0)
    expect(clampGravity(-1)).toBe(0)
    expect(clampGravity(0)).toBe(0)
    expect(clampGravity(0.5)).toBe(0.5)
    expect(clampGravity(1)).toBe(1)
    expect(clampGravity(7)).toBe(1)
  })
})

// ─────────────────────────────────────────────────────────────────────
// WebGL setup contract — fake GL context
// ─────────────────────────────────────────────────────────────────────

interface FakeShader {
  type: number
  source?: string
  compileOk: boolean
  deleted: boolean
}

interface FakeProgram {
  shaders: FakeShader[]
  linkOk: boolean
  deleted: boolean
}

interface FakeBuffer {
  data?: BufferSource
  deleted: boolean
}

function makeFakeGl(opts: { compileOk?: boolean; linkOk?: boolean; createOk?: boolean } = {}) {
  const compileOk = opts.compileOk ?? true
  const linkOk = opts.linkOk ?? true
  const createOk = opts.createOk ?? true

  const gl = {
    VERTEX_SHADER: 0x8b31,
    FRAGMENT_SHADER: 0x8b30,
    ARRAY_BUFFER: 0x8892,
    STATIC_DRAW: 0x88e4,
    COMPILE_STATUS: 0x8b81,
    LINK_STATUS: 0x8b82,
    FLOAT: 0x1406,
    TRIANGLES: 0x4,
    createShader(type: number): FakeShader | null {
      if (!createOk) return null
      return { type, compileOk, deleted: false }
    },
    shaderSource(shader: FakeShader, src: string) {
      shader.source = src
    },
    compileShader(_s: FakeShader) {},
    getShaderParameter(s: FakeShader, _p: number) {
      return s.compileOk
    },
    deleteShader(s: FakeShader) {
      s.deleted = true
    },
    createProgram(): FakeProgram | null {
      if (!createOk) return null
      return { shaders: [], linkOk, deleted: false }
    },
    attachShader(p: FakeProgram, s: FakeShader) {
      p.shaders.push(s)
    },
    linkProgram(_p: FakeProgram) {},
    detachShader(_p: FakeProgram, _s: FakeShader) {},
    deleteProgram(p: FakeProgram) {
      p.deleted = true
    },
    getProgramParameter(p: FakeProgram, _q: number) {
      return p.linkOk
    },
    createBuffer(): FakeBuffer | null {
      if (!createOk) return null
      return { deleted: false }
    },
    bindBuffer(_t: number, _b: FakeBuffer) {},
    bufferData(_t: number, data: BufferSource, _u: number) {
      // captured for assertions
      ;(this as unknown as { _lastBufferData: BufferSource })._lastBufferData = data
    },
    deleteBuffer(b: FakeBuffer) {
      b.deleted = true
    },
    getAttribLocation(_p: FakeProgram, _name: string) {
      return 0
    },
    getUniformLocation(_p: FakeProgram, name: string) {
      // Uniform locations are opaque handles; return a marker object
      // so we can verify the leaf passes them through.
      return { _name: name }
    },
    viewport(..._args: number[]) {},
    useProgram(_p: FakeProgram | null) {},
    enableVertexAttribArray(_loc: number) {},
    vertexAttribPointer(..._args: unknown[]) {},
    uniform1f(..._args: unknown[]) {},
    uniform2f(..._args: unknown[]) {},
    uniform1i(..._args: unknown[]) {},
    drawArrays: vi.fn(),
  }
  return gl as unknown as WebGLRenderingContext & {
    drawArrays: ReturnType<typeof vi.fn>
    _lastBufferData?: BufferSource
  }
}

describe("AS.7.0 nebula-shader — WebGL setup", () => {
  it("setupNebulaProgram returns a populated handle on success", () => {
    const gl = makeFakeGl()
    const prog = setupNebulaProgram(gl)
    expect(prog).not.toBeNull()
    expect(prog?.uniforms.time).toMatchObject({ _name: "u_time" })
    expect(prog?.uniforms.resolution).toMatchObject({ _name: "u_resolution" })
    expect(prog?.uniforms.mouse).toMatchObject({ _name: "u_mouse" })
    expect(prog?.uniforms.starLayers).toMatchObject({ _name: "u_starLayers" })
    expect(prog?.uniforms.gravity).toMatchObject({ _name: "u_gravity" })
    // Three-vertex full-screen triangle — six floats.
    const data = (gl as unknown as { _lastBufferData: Float32Array })._lastBufferData
    expect(data).toBeInstanceOf(Float32Array)
    expect((data as Float32Array).length).toBe(6)
  })

  it("setupNebulaProgram returns null when shader compile fails", () => {
    const gl = makeFakeGl({ compileOk: false })
    expect(setupNebulaProgram(gl)).toBeNull()
  })

  it("setupNebulaProgram returns null when program link fails", () => {
    const gl = makeFakeGl({ linkOk: false })
    expect(setupNebulaProgram(gl)).toBeNull()
  })

  it("setupNebulaProgram returns null when GL resource creation fails", () => {
    const gl = makeFakeGl({ createOk: false })
    expect(setupNebulaProgram(gl)).toBeNull()
  })

  it("drawNebulaFrame issues exactly one drawArrays call", () => {
    const gl = makeFakeGl()
    const prog = setupNebulaProgram(gl)!
    drawNebulaFrame(prog, {
      timeSeconds: 1.5,
      resolutionPx: { width: 800, height: 600 },
      mouseUv: { x: 0.5, y: 0.5 },
      starLayers: 3,
      gravityStrength: 1,
    })
    expect(gl.drawArrays).toHaveBeenCalledTimes(1)
  })

  it("cleanupNebulaProgram releases program + buffer", () => {
    const gl = makeFakeGl()
    const prog = setupNebulaProgram(gl)!
    cleanupNebulaProgram(prog)
    // Cast to access deleted flag from the fake.
    expect((prog.program as unknown as FakeProgram).deleted).toBe(true)
    expect((prog.positionBuffer as unknown as FakeBuffer).deleted).toBe(true)
  })
})
