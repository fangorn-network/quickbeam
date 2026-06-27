// Back-compat shim. UI microcopy is now part of the active locale profile
// (lib/i18n), so COPY is the translated string pack for the selected VITE_LOCALE.
export type { Strings } from './i18n';
export { COPY } from './i18n';
