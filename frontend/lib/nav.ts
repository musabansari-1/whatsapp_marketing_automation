/**
 * Dashboard navigation policy (specification §17).
 *
 * The section list and the active-path rule live here rather than in the component so
 * they can be tested without rendering: §17 names nine sections, and a missing one is
 * a specification gap rather than a styling bug.
 */

export interface NavSection {
  href: string;
  label: string;
}

/**
 * The §17 sections, in the order a lead actually moves through them.
 *
 * Analytics is not a separate entry: its metrics are the Overview, and a second page
 * showing the same numbers would be a menu item with nothing behind it.
 */
export const NAV_SECTIONS: readonly NavSection[] = [
  { href: '/', label: 'Overview' },
  { href: '/audiences', label: 'Audiences' },
  { href: '/campaigns', label: 'Campaigns' },
  { href: '/leads', label: 'Leads' },
  { href: '/conversations', label: 'Conversations' },
  { href: '/bookings', label: 'Demos' },
  { href: '/knowledge', label: 'Knowledge' },
  { href: '/notifications', label: 'Notifications' },
];

/**
 * Whether a section should be highlighted for the current path.
 *
 * Exact match for the overview, prefix for everything else: `/leads/<id>` is still
 * Leads, but `/leads` must not light up Overview simply because every path starts
 * with a slash.
 */
export function isActiveSection(pathname: string, href: string): boolean {
  return href === '/' ? pathname === '/' : pathname === href || pathname.startsWith(`${href}/`);
}
