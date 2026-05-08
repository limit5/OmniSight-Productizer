# Docs Site Pipeline (OP-792)

`.github/workflows/docs-site-publish.yml` publishes the static docs site on
every push to `develop`, which is the repository signal for a completed
develop merge.

## Build Flow

1. Check out the merged `develop` tree.
2. Install Node framework dependencies with `pnpm install --frozen-lockfile
   --prefer-offline`.
3. Restore the previous `docs-site-dist` artifact cache when the docs hash
   allows it.
4. Run `python -m backend.docs_static_site --out docs-site-dist`.
5. Upload the static artifact and deploy it with GitHub Pages.

The build job has a 5-minute timeout. The deploy job depends on the build job,
so a build failure never updates GitHub Pages; the previous successful Pages deployment remains live.

## Failure Handling

When the build job fails, the `notify-operator` job writes a GitHub Actions
error annotation and run summary with the failed run URL. That notification is
the operator signal to inspect the build log and fix the docs source or builder.

## Local Check

```bash
python -m backend.docs_static_site --out /tmp/omnisight-docs-site
```

Open `/tmp/omnisight-docs-site/index.html` to inspect the generated artifact.
