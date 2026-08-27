/**
 * Dashboard navigation (specification §17).
 *
 * Kept as a thin re-export so existing imports continue to work. The live chrome
 * is `AppShell`, which still reads the section list from `lib/nav.ts`.
 */

export { AppShell as Nav } from '@/components/shell';
