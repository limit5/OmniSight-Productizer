// AppTile — one holo-glass tile per app, the web analogue of
// launcher-qt/qml/components/AppTile.qml (U1.3).
//
// AC-Code: holo-glass surface (translucent fill, --color-tile-bg) +
//          neural-blue border (--color-tile-border) + neural-blue focus
//          glow (--color-tile-focus-ring + --color-neural-blue-glow) +
//          category accent stripe + category-tinted icon chip. Every
//          color/radius/size is a CSS var emitted from the U2.1 generated
//          @theme block — no raw hex / px literals here.
// AC-Integration: emits onActivate(appId) only. Tile click -> navigate
//                 / launch is U2.4 territory; do NOT add it here.
'use client';

import type { CSSProperties, KeyboardEvent, ReactElement } from 'react';

import { resolveIcon } from '../lib/icons';
import type { AppCategory, FilteredApp } from '../lib/types';

export interface AppTileProps {
  app: FilteredApp;
  /**
   * CSS expression evaluated by the browser as the tile accent. Pass a
   * design-token reference (e.g. `'var(--color-cat-core)'`) so the brand
   * stays sourced from design-tokens.json.
   */
  accent: string;
  /**
   * Emitted on click / Enter / Space. Does NOT navigate or launch — that
   * wiring lands in U2.4.
   */
  onActivate?: (appId: string) => void;
}

function categoryLabel(cat: AppCategory): string {
  return cat.charAt(0).toUpperCase() + cat.slice(1);
}

export function AppTile({ app, accent, onActivate }: AppTileProps): ReactElement {
  const Icon = resolveIcon(app.icon);

  const handleActivate = (): void => {
    onActivate?.(app.id);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLButtonElement>): void => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      handleActivate();
    }
  };

  // CSS custom property carries the accent into every nested element
  // (stripe, icon chip background, focus glow tint variants) without
  // forcing per-category style blocks.
  const style: CSSProperties = {
    ['--tile-accent' as string]: accent,
  };

  return (
    <button
      type="button"
      className="app-tile"
      data-testid={`app-tile-${app.id}`}
      data-app-id={app.id}
      data-category={app.category}
      style={style}
      onClick={handleActivate}
      onKeyDown={handleKeyDown}
      aria-label={`${app.title} — ${categoryLabel(app.category)}`}
    >
      <span className="app-tile-accent-stripe" aria-hidden="true" />
      <span className="app-tile-icon" aria-hidden="true">
        <Icon strokeWidth={1.75} />
      </span>
      <span className="app-tile-title">{app.title}</span>
    </button>
  );
}

export default AppTile;
