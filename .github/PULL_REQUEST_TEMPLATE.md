<!-- Keep the title ≤72 chars; it becomes the squash-merge commit subject. -->

## What

<!-- One paragraph, user-facing: what changed and why. -->

## How

<!-- Bullet the non-obvious design choices and any rejected alternatives. -->

## Audit ids addressed

<!-- Format: `R2 #20`, `B12`, `A4/C7`, or `N/A`. -->

## Test plan

- [ ] Unit: `pytest backend/tests/<relevant>` — pasted output
- [ ] Frontend: `npx vitest run test/` — 52/52 green
- [ ] Manual: describe the UI scenario you clicked through

## Checklist

- [ ] `.env.example` updated if a new `OMNISIGHT_*` var was added
- [ ] `CHANGELOG.md` `Unreleased` section updated
- [ ] No `@ts-ignore` / `# type: ignore` / `eslint-disable` without a
      justifying comment
- [ ] No secrets in the diff
- [ ] Dark theme preserved (no hard-coded light hex)
