// FX.7.11 — next-intl server request config.
//
// `createNextIntlPlugin()` in `next.config.mjs` points at this file as
// the request-scoped resolver. For each server render we:
//   1. Read the locale cookie (`omnisight-locale`) — set by the client
//      `I18nProvider` whenever the user picks a language.
//   2. Fall back to `DEFAULT_LOCALE` when the cookie is absent or holds
//      an unknown value.
//   3. Load the matching JSON bundle from `messages/<locale>.json`.
//
// The dynamic `import()` is statically analysable by webpack/turbopack
// when the path includes a literal extension, so all four bundles still
// get bundled into the server build — no runtime FS read.

import { getRequestConfig } from "next-intl/server";
import { cookies } from "next/headers";
import { DEFAULT_LOCALE, LOCALE_COOKIE, isLocale, type Locale } from "./routing";

export default getRequestConfig(async () => {
  let locale: Locale = DEFAULT_LOCALE;

  try {
    const store = await cookies();
    const cookieValue = store.get(LOCALE_COOKIE)?.value;
    if (isLocale(cookieValue)) {
      locale = cookieValue;
    }
  } catch {
    // `cookies()` throws when called outside a request scope (e.g. when
    // next-intl is imported during static analysis). Falling back to the
    // default locale is correct in that case.
  }

  const messages = (await import(`../messages/${locale}.json`)).default;

  return {
    locale,
    messages,
  };
});
