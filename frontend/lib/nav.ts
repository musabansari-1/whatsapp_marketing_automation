/**
 * Dashboard navigation policy (specification §17, §28).
 *
 * Two lists, because the dashboard has two kinds of page. Product sections live under
 * `/p/{slug}` and follow the product switcher; organisation sections belong to everyone
 * whichever product they are working in. Kept out of the component so the lists and the
 * highlighting rule can be tested without rendering: a missing section is a
 * specification gap, not a styling bug.
 */

export interface NavSection {
  /** Stable identifier, used for icons and tests. */
  key: string;
  /** For product sections, the path inside the product ('' is its overview). */
  path: string;
  label: string;
}

/** The §17 sections, per product, in the order a lead moves through them. */
export const PRODUCT_SECTIONS: readonly NavSection[] = [
  { key: 'overview', path: '', label: 'Overview' },
  { key: 'audiences', path: '/audiences', label: 'Audiences' },
  { key: 'campaigns', path: '/campaigns', label: 'Campaigns' },
  { key: 'templates', path: '/templates', label: 'Templates' },
  { key: 'leads', path: '/leads', label: 'Leads' },
  { key: 'conversations', path: '/conversations', label: 'Conversations' },
  { key: 'bookings', path: '/bookings', label: 'Demos' },
  { key: 'knowledge', path: '/knowledge', label: 'Knowledge' },
];

/** Pages for the whole organisation. */
export const GLOBAL_SECTIONS: readonly NavSection[] = [
  { key: 'products', path: '/', label: 'All products' },
  { key: 'notifications', path: '/notifications', label: 'Notifications' },
];

/**
 * Whether a section should be highlighted.
 *
 * `current` and `section` must be in the same space: both paths inside a product (whose
 * overview is '') or both global paths (whose home is '/'). The root matches only
 * exactly — every path starts with it — and anything else by prefix, so `/leads/<id>` is
 * still Leads while `/leadsomething` is not.
 */
export function isActiveSection(current: string, section: string): boolean {
  if (section === '' || section === '/') {
    return current === section;
  }
  return current === section || current.startsWith(`${section}/`);
}

/** The section a path belongs to, if any. */
export function sectionFor(
  current: string,
  sections: readonly NavSection[],
): NavSection | undefined {
  return sections.find((section) => isActiveSection(current, section.path));
}
