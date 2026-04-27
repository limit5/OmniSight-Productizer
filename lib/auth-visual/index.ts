/**
 * AS.7.0 — Auth Visual Foundation public surface.
 *
 * Re-exports the pure helpers + GLSL source pair so AS.7.1..AS.7.8
 * page implementations can `import { ... } from "@/lib/auth-visual"`
 * without reaching into the individual files. The React component
 * primitives live under `@/components/omnisight/auth/...` and are
 * intentionally NOT re-exported here — Tree-shaking React leaves
 * works better if pages import them directly from the component
 * paths, and keeping this barrel pure-helper-only means SSR
 * imports of motion-policy don't accidentally pull in the WebGL
 * canvas component.
 */

export {
  AUTH_VISUAL_BUDGET_TABLE,
  getAuthVisualBudget,
  type AuthVisualBudget,
} from "./motion-policy"

export {
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
  type NebulaFrameUniforms,
  type NebulaProgram,
} from "./nebula-shader"

export {
  buildGlassCardTransform,
  idleDriftOffsetPx,
  scrollParallaxOffsetPx,
  tiltFromPointer,
  type GlassCardTilt,
} from "./glass-card-physics"
