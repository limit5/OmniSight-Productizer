/**
 * AS.7.0 — Nebula background WebGL shader.
 *
 * Owns the GLSL source pair (vertex + fragment), the WebGL program
 * setup helper, and the per-frame uniform updater. The shader
 * implements the AS.7.0 spec:
 *
 *   - Slow nebula gradient drift driven by a `u_time` uniform.
 *   - Three independently-parallaxed star layers with the layer
 *     count gated on `u_starLayers` (so the same shader binary
 *     handles every motion level — we just dial layers down at
 *     normal / off via the uniform rather than swapping programs).
 *   - Pointer gravity-well: pixels near the cursor get pulled
 *     inward by `u_gravityStrength`, producing the "the nebula
 *     bends toward you" effect.
 *
 * The fragment-shader main body is intentionally kept under ~50
 * non-blank GLSL lines (per AS.7.0 row spec "~50 行 GLSL"). A
 * grep-able marker (`// AS.7.0 fragment-main begin/end`) brackets
 * the body so a regression-guard test can pin the line count and
 * fail loudly if it drifts beyond budget.
 *
 * Module-global state audit (per docs/sop/implement_phase_step.md
 * Step 1):
 *
 *   - GLSL source strings + drift markers are module-level
 *     immutable strings — no mutation, every uvicorn worker /
 *     browser tab derives identical output (Answer #1).
 *   - The `setupNebulaProgram` factory creates one GLProgram per
 *     call; ownership of the WebGL resources transfers to the
 *     caller, which is responsible for `cleanup()` on unmount.
 *     No global GL state is cached in this module.
 *   - We deliberately do NOT use a singleton `program` cache —
 *     two auth pages mounted simultaneously (impossible today,
 *     but defensible) each get their own program, avoiding any
 *     "second mount steals the GL context" footgun.
 *
 * Read-after-write timing audit: N/A — single-threaded JS plus
 * the GPU. The `requestAnimationFrame` loop reads uniforms after
 * the caller sets them, so there's no read-after-write race.
 *
 * SSR safety: this module is `import`-safe even on the server —
 * none of the top-level statements touch `window` / `document`.
 * The setup function takes a `WebGLRenderingContext` from the
 * caller, so it's the caller's job to gate the call behind a
 * client-only mount.
 */

// ─────────────────────────────────────────────────────────────────────
// GLSL source
// ─────────────────────────────────────────────────────────────────────

/** Vertex shader — full-screen triangle. The fragment shader does
 *  every pixel calculation, so the vertex side is intentionally
 *  trivial. Three vertices cover the [-1, 1] clip space. */
export const NEBULA_VERTEX_SHADER = /* glsl */ `
attribute vec2 a_position;
varying vec2 v_uv;
void main() {
  v_uv = a_position * 0.5 + 0.5;
  gl_Position = vec4(a_position, 0.0, 1.0);
}
`.trim()

/** Marker bracketing the fragment-shader main body. The drift-
 *  guard test in `nebula-shader.test.ts` greps for these
 *  literals + counts the lines between them, so renaming or
 *  removing them is a CI-red break. */
export const NEBULA_FRAGMENT_MAIN_BEGIN = "// AS.7.0 fragment-main begin"
export const NEBULA_FRAGMENT_MAIN_END = "// AS.7.0 fragment-main end"

/**
 * Fragment shader. Single-pass nebula + 3 star layers + pointer
 * gravity well. Uniforms:
 *
 *   - `u_time`        elapsed seconds since program creation
 *   - `u_resolution`  canvas size (px) — for aspect-correct UVs
 *   - `u_mouse`       pointer position in normalised UV (0..1, y
 *                     flipped so origin is bottom-left to match
 *                     gl_FragCoord)
 *   - `u_starLayers`  integer 0..3 — gates how many star layers
 *                     are rendered (BS.3.4 / AS.7.0 budget)
 *   - `u_gravity`     0..1 strength of the cursor gravity well
 */
export const NEBULA_FRAGMENT_SHADER = /* glsl */ `
precision mediump float;

uniform float u_time;
uniform vec2  u_resolution;
uniform vec2  u_mouse;
uniform int   u_starLayers;
uniform float u_gravity;

varying vec2 v_uv;

float hash(vec2 p) {
  return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  float a = hash(i);
  float b = hash(i + vec2(1.0, 0.0));
  float c = hash(i + vec2(0.0, 1.0));
  float d = hash(i + vec2(1.0, 1.0));
  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

float fbm(vec2 p) {
  float v = 0.0;
  float amp = 0.5;
  for (int i = 0; i < 4; i++) {
    v += amp * noise(p);
    p *= 2.03;
    amp *= 0.5;
  }
  return v;
}

float starLayer(vec2 uv, float density, float speed, float seed) {
  vec2 p = uv * density + vec2(u_time * speed, seed);
  vec2 i = floor(p);
  vec2 f = fract(p);
  float h = hash(i);
  float twinkle = 0.6 + 0.4 * sin(u_time * 2.0 + h * 6.2831);
  float d = length(f - 0.5);
  float star = smoothstep(0.06, 0.0, d) * step(0.985, h);
  return star * twinkle;
}

// AS.7.0 fragment-main begin
void main() {
  vec2 uv = v_uv;
  vec2 aspect = vec2(u_resolution.x / max(u_resolution.y, 1.0), 1.0);
  vec2 centred = (uv - 0.5) * aspect;
  vec2 mouseAspect = (u_mouse - 0.5) * aspect;
  vec2 toMouse = centred - mouseAspect;
  float md = length(toMouse);
  float pull = u_gravity * exp(-md * 4.0) * 0.08;
  vec2 warped = uv - normalize(toMouse + 1e-4) * pull;
  float drift = u_time * 0.015;
  float n1 = fbm(warped * 1.4 + vec2(drift, drift * 0.6));
  float n2 = fbm(warped * 3.1 - vec2(drift * 0.7, drift));
  vec3 col = mix(vec3(0.02, 0.04, 0.10), vec3(0.05, 0.10, 0.22), n1);
  col += vec3(0.18, 0.10, 0.32) * pow(n2, 2.5) * 0.6;
  col += vec3(0.08, 0.20, 0.30) * pow(n1, 3.0) * 0.5;
  float stars = 0.0;
  if (u_starLayers >= 1) stars += starLayer(uv, 80.0,  0.004, 17.0);
  if (u_starLayers >= 2) stars += starLayer(uv, 140.0, 0.010, 53.0) * 0.7;
  if (u_starLayers >= 3) stars += starLayer(uv, 220.0, 0.022, 91.0) * 0.5;
  col += vec3(stars);
  float vignette = smoothstep(1.2, 0.4, length(centred));
  col *= mix(0.6, 1.0, vignette);
  gl_FragColor = vec4(col, 1.0);
}
// AS.7.0 fragment-main end
`.trim()

// ─────────────────────────────────────────────────────────────────────
// Program setup
// ─────────────────────────────────────────────────────────────────────

/** Resources owned by a compiled nebula program. The caller stores
 *  this opaque handle and feeds it to `drawNebulaFrame` /
 *  `cleanupNebulaProgram` for the lifetime of the canvas. */
export interface NebulaProgram {
  gl: WebGLRenderingContext
  program: WebGLProgram
  positionBuffer: WebGLBuffer
  positionLocation: number
  uniforms: {
    time: WebGLUniformLocation | null
    resolution: WebGLUniformLocation | null
    mouse: WebGLUniformLocation | null
    starLayers: WebGLUniformLocation | null
    gravity: WebGLUniformLocation | null
  }
}

/** Per-frame uniform values passed to `drawNebulaFrame`. */
export interface NebulaFrameUniforms {
  /** Elapsed seconds since program creation. */
  timeSeconds: number
  /** Canvas size in physical pixels. */
  resolutionPx: { width: number; height: number }
  /** Pointer position in [0, 1] UV space (origin bottom-left to
   *  match `gl_FragCoord`). Use `{ x: 0.5, y: 0.5 }` when no
   *  pointer is hovering. */
  mouseUv: { x: number; y: number }
  /** Number of star layers to render — clamped to 0..3. */
  starLayers: number
  /** Pointer gravity-well strength in 0..1. */
  gravityStrength: number
}

/** Compile + link a GLSL shader. Returns null on failure so the
 *  caller can degrade to the static CSS gradient instead of
 *  exploding the auth page. */
function compileShader(
  gl: WebGLRenderingContext,
  type: number,
  source: string,
): WebGLShader | null {
  const shader = gl.createShader(type)
  if (!shader) return null
  gl.shaderSource(shader, source)
  gl.compileShader(shader)
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    gl.deleteShader(shader)
    return null
  }
  return shader
}

/**
 * Build a nebula GL program. Returns `null` when any step fails
 * (no GL context, shader compile error, link error) — caller
 * MUST handle this and fall back to the static CSS background.
 *
 * The position buffer is bound on creation so per-frame
 * `drawNebulaFrame` only needs to update uniforms and `drawArrays`,
 * not re-bind state.
 */
export function setupNebulaProgram(gl: WebGLRenderingContext): NebulaProgram | null {
  const vert = compileShader(gl, gl.VERTEX_SHADER, NEBULA_VERTEX_SHADER)
  if (!vert) return null
  const frag = compileShader(gl, gl.FRAGMENT_SHADER, NEBULA_FRAGMENT_SHADER)
  if (!frag) {
    gl.deleteShader(vert)
    return null
  }
  const program = gl.createProgram()
  if (!program) {
    gl.deleteShader(vert)
    gl.deleteShader(frag)
    return null
  }
  gl.attachShader(program, vert)
  gl.attachShader(program, frag)
  gl.linkProgram(program)
  // Detach + delete shaders even on success — the linked program
  // keeps its own copy, and leaving the source attached pins extra
  // memory on the GPU side.
  gl.detachShader(program, vert)
  gl.detachShader(program, frag)
  gl.deleteShader(vert)
  gl.deleteShader(frag)
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    gl.deleteProgram(program)
    return null
  }
  const positionBuffer = gl.createBuffer()
  if (!positionBuffer) {
    gl.deleteProgram(program)
    return null
  }
  // Three vertices for an oversized triangle that covers [-1, 1]^2.
  const triangle = new Float32Array([-1, -1, 3, -1, -1, 3])
  gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer)
  gl.bufferData(gl.ARRAY_BUFFER, triangle, gl.STATIC_DRAW)
  const positionLocation = gl.getAttribLocation(program, "a_position")
  return {
    gl,
    program,
    positionBuffer,
    positionLocation,
    uniforms: {
      time: gl.getUniformLocation(program, "u_time"),
      resolution: gl.getUniformLocation(program, "u_resolution"),
      mouse: gl.getUniformLocation(program, "u_mouse"),
      starLayers: gl.getUniformLocation(program, "u_starLayers"),
      gravity: gl.getUniformLocation(program, "u_gravity"),
    },
  }
}

/** Render one frame. Caller owns the rAF loop + frame-rate cap. */
export function drawNebulaFrame(prog: NebulaProgram, frame: NebulaFrameUniforms): void {
  const { gl, program, positionBuffer, positionLocation, uniforms } = prog
  gl.viewport(0, 0, frame.resolutionPx.width, frame.resolutionPx.height)
  gl.useProgram(program)
  gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer)
  if (positionLocation >= 0) {
    gl.enableVertexAttribArray(positionLocation)
    gl.vertexAttribPointer(positionLocation, 2, gl.FLOAT, false, 0, 0)
  }
  if (uniforms.time) gl.uniform1f(uniforms.time, frame.timeSeconds)
  if (uniforms.resolution) {
    gl.uniform2f(uniforms.resolution, frame.resolutionPx.width, frame.resolutionPx.height)
  }
  if (uniforms.mouse) gl.uniform2f(uniforms.mouse, frame.mouseUv.x, frame.mouseUv.y)
  if (uniforms.starLayers) {
    gl.uniform1i(uniforms.starLayers, clampStarLayers(frame.starLayers))
  }
  if (uniforms.gravity) {
    gl.uniform1f(uniforms.gravity, clampGravity(frame.gravityStrength))
  }
  gl.drawArrays(gl.TRIANGLES, 0, 3)
}

/** Free GL resources owned by a program. After calling this the
 *  `NebulaProgram` handle is unusable. */
export function cleanupNebulaProgram(prog: NebulaProgram): void {
  const { gl, program, positionBuffer } = prog
  gl.deleteBuffer(positionBuffer)
  gl.deleteProgram(program)
}

/** Clamp the star-layer count to the shader's supported range. */
export function clampStarLayers(n: number): 0 | 1 | 2 | 3 {
  if (!Number.isFinite(n) || n <= 0) return 0
  if (n >= 3) return 3
  return Math.round(n) as 1 | 2 | 3
}

/** Clamp the gravity strength to [0, 1]. */
export function clampGravity(g: number): number {
  if (!Number.isFinite(g) || g <= 0) return 0
  if (g >= 1) return 1
  return g
}

// ─────────────────────────────────────────────────────────────────────
// Drift-guard helper — used by `nebula-shader.test.ts`
// ─────────────────────────────────────────────────────────────────────

/** Count non-blank GLSL lines between the begin / end markers in
 *  `NEBULA_FRAGMENT_SHADER`. Returns -1 if either marker is
 *  missing — the test asserts > 0 and ≤ 60 to leave a small
 *  margin around the AS.7.0 "~50 行 GLSL" budget. */
export function nebulaFragmentMainLineCount(): number {
  const src = NEBULA_FRAGMENT_SHADER
  const begin = src.indexOf(NEBULA_FRAGMENT_MAIN_BEGIN)
  const end = src.indexOf(NEBULA_FRAGMENT_MAIN_END)
  if (begin < 0 || end < 0 || end <= begin) return -1
  const body = src.slice(begin + NEBULA_FRAGMENT_MAIN_BEGIN.length, end)
  return body
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0).length
}
