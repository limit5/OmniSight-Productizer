// CategoryStrip — section header for one category in the home grid.
// Mirrors the launcher-qt HomeScreen section header (accent pill +
// Orbitron label + app count) so a 7" panel and a 4K display surface
// the same visual rhythm.
//
// Accent comes in as a CSS expression (e.g. `var(--color-cat-core)`)
// rather than a category enum lookup, so this component stays brand-
// agnostic and category-to-color mapping lives in one place
// (AppGrid -> design-tokens via --color-cat-*).
import type { CSSProperties, ReactElement } from 'react';

import type { AppCategory } from '../lib/types';

export interface CategoryStripProps {
  category: AppCategory;
  /** Display label (e.g. "Core"). */
  label: string;
  /** CSS expression for the accent (e.g. `'var(--color-cat-core)'`). */
  accent: string;
  /** App count badge — shown to the right of the label. */
  count: number;
}

export function CategoryStrip({
  category,
  label,
  accent,
  count,
}: CategoryStripProps): ReactElement {
  const style: CSSProperties = {
    ['--tile-accent' as string]: accent,
  };
  return (
    <header
      className="category-strip"
      data-testid={`category-strip-${category}`}
      data-category={category}
      style={style}
    >
      <span className="category-strip-accent" aria-hidden="true" />
      <h2 id={`category-label-${category}`} className="category-strip-label">
        {label}
      </h2>
      <span className="category-strip-count" aria-label={`${count} apps`}>
        {count} app{count === 1 ? '' : 's'}
      </span>
    </header>
  );
}

export default CategoryStrip;
