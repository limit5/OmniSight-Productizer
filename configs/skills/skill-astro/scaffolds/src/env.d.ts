/// <reference path="../.astro/types.d.ts" />
/// <reference types="astro/client" />

interface ImportMetaEnv {
  readonly SITE_URL: string
  readonly SANITY_PROJECT_ID?: string
  readonly SANITY_DATASET?: string
  readonly SANITY_API_VERSION?: string
  readonly SANITY_PREVIEW_TOKEN?: string
  readonly SANITY_WEBHOOK_SECRET?: string
  readonly CONTENTFUL_SPACE_ID?: string
  readonly CONTENTFUL_ENVIRONMENT?: string
  readonly CONTENTFUL_DELIVERY_TOKEN?: string
  readonly CONTENTFUL_PREVIEW_TOKEN?: string
  readonly CONTENTFUL_WEBHOOK_SECRET?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
