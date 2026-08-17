// Back-compat shim. Per-community branding now lives in the locale registry
// (lib/i18n), selected by VITE_LOCALE and overridable per-field via VITE_COMMUNITY_*.
// Existing imports of COMMUNITY / communityChip / communityFull keep working.
export type { Community } from './i18n';
export { COMMUNITY, communityChip, communityFull } from './i18n';
