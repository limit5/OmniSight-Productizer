// Manifest `icon` id -> lucide-react component, resolved through the
// design-tokens.json alias map so launcher-qt and launcher-web pick the
// same glyph for the same manifest id (U1.1 + U2 contract).
//
// The alias map (design-system/design-tokens.json `icons.aliases`) is the
// SOLE source for "id -> lucide name"; we bundle it at build time via
// tsconfig.compilerOptions.resolveJsonModule so the static SPA needs no
// runtime fetch for icon mapping. The lucide-react components for each
// reachable target are individually imported (named imports keep the
// bundle small and tree-shakeable — no `import * as Lucide` here).
//
// If a manifest id is NOT in the alias map we treat it as a raw lucide
// kebab-case name and look it up directly (e.g. example manifest's
// `live-view` uses `icon: video`). Unknown ids fall back to LayoutGrid so
// a missing glyph never crashes the tile render.
import {
  Activity,
  Camera,
  Folder,
  HardDrive,
  LayoutGrid,
  Network,
  Phone,
  PlayCircle,
  Settings,
  Shield,
  ShoppingCart,
  Video,
  Wrench,
  type LucideIcon,
} from 'lucide-react';

import tokens from '../../design-system/design-tokens.json';

// Lucide kebab-case name -> React component. Curated to the targets the
// alias map currently points at + the raw lucide names that appear in
// design-system/apps.manifest.example.yaml. Adding a new alias target
// here is a one-line edit and the launcher-qt icon provider follows the
// same list.
const LUCIDE_BY_NAME: Readonly<Record<string, LucideIcon>> = {
  activity: Activity,
  camera: Camera,
  folder: Folder,
  'hard-drive': HardDrive,
  network: Network,
  phone: Phone,
  'play-circle': PlayCircle,
  settings: Settings,
  shield: Shield,
  'shopping-cart': ShoppingCart,
  video: Video,
  wrench: Wrench,
};

// Manifest id -> lucide kebab-case name. Loaded from design-tokens.json so
// the two renderers (Qt icon provider, web React component) never drift.
const ALIASES: Readonly<Record<string, string>> = tokens.icons.aliases;

/**
 * Resolve a manifest `icon` id to a renderable lucide React component.
 * Falls back to {@link LayoutGrid} when the id is unknown so a typo in
 * apps.manifest never wipes out the tile.
 */
export function resolveIcon(iconId: string): LucideIcon {
  const lucideName = ALIASES[iconId] ?? iconId;
  return LUCIDE_BY_NAME[lucideName] ?? LayoutGrid;
}
