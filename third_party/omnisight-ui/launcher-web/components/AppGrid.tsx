// AppGrid — home tile grid for the web launcher (U2.3).
// Web analogue of launcher-qt HomeScreen + AdaptiveGrid + per-category
// SwipeView page.
//
// AC-Code: reads the U2.2 filtered AppList model, groups by category in
//          schema enum order (core, media, tools, diagnostics, settings),
//          renders one CategoryStrip + one auto-fill CSS grid of AppTiles
//          per category. Each tile carries the category accent
//          (--color-cat-<category>, sourced from design-tokens.json via
//          the U2.1 generated @theme block) — no raw hex / size literals
//          live here.
// MUST NOT: do NOT launch / navigate on tile activate (U2.4). AppGrid
//           simply forwards onActivate(appId) from each tile.
import type { ReactElement } from 'react';

import { AppTile } from './AppTile';
import { CategoryStrip } from './CategoryStrip';
import type { AppCategory, FilteredApp } from '../lib/types';

export interface AppGridProps {
  apps: readonly FilteredApp[];
  onActivate?: (appId: string) => void;
}

// Mirrors apps.manifest.schema.json $defs.app.category enum ordering and
// matches lib/manifest.ts CATEGORY_ORDER. Apps with an unknown category
// trail the known set (shouldn't happen post-ajv-validate).
const CATEGORY_ORDER: readonly AppCategory[] = [
  'core',
  'media',
  'communication',
  'tools',
  'diagnostics',
  'settings',
];

// `--color-cat-<category>` are emitted by gen_web_theme.py from
// design-tokens.json `category_accent` — see app/globals.css. We pass
// the CSS expression rather than reading the resolved color so the
// React layer never has to know the literal hex.
function accentForCategory(cat: AppCategory): string {
  return `var(--color-cat-${cat})`;
}

function labelForCategory(cat: AppCategory): string {
  return cat.charAt(0).toUpperCase() + cat.slice(1);
}

interface Section {
  category: AppCategory;
  label: string;
  accent: string;
  apps: readonly FilteredApp[];
}

function groupByCategory(apps: readonly FilteredApp[]): Section[] {
  const buckets = new Map<AppCategory, FilteredApp[]>();
  for (const a of apps) {
    let bucket = buckets.get(a.category);
    if (!bucket) {
      bucket = [];
      buckets.set(a.category, bucket);
    }
    bucket.push(a);
  }
  const sections: Section[] = [];
  for (const cat of CATEGORY_ORDER) {
    const list = buckets.get(cat);
    if (list && list.length > 0) {
      sections.push({
        category: cat,
        label: labelForCategory(cat),
        accent: accentForCategory(cat),
        apps: list,
      });
    }
  }
  // Trailing fallback: any category the schema doesn't know about (would
  // only fire if a future schema enum addition lands before CATEGORY_ORDER
  // is updated). Better to render than to drop the tile.
  for (const [cat, list] of buckets) {
    if (CATEGORY_ORDER.indexOf(cat) >= 0) continue;
    sections.push({
      category: cat,
      label: labelForCategory(cat),
      accent: accentForCategory(cat),
      apps: list,
    });
  }
  return sections;
}

export function AppGrid({ apps, onActivate }: AppGridProps): ReactElement {
  const sections = groupByCategory(apps);

  if (sections.length === 0) {
    return (
      <div className="home-empty" data-testid="app-grid-empty" role="status">
        No apps visible for this device / role.
      </div>
    );
  }

  return (
    <div className="app-grid" data-testid="app-grid" role="list">
      {sections.map((section) => (
        <section
          key={section.category}
          className="category-section"
          data-testid={`category-section-${section.category}`}
          aria-labelledby={`category-label-${section.category}`}
        >
          <CategoryStrip
            category={section.category}
            label={section.label}
            accent={section.accent}
            count={section.apps.length}
          />
          <div
            className="app-tiles"
            data-testid={`app-tiles-${section.category}`}
            role="list"
          >
            {section.apps.map((app) => (
              <div role="listitem" key={app.id}>
                <AppTile app={app} accent={section.accent} onActivate={onActivate} />
              </div>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

export default AppGrid;
