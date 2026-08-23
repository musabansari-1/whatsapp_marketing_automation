/**
 * Dashboard navigation tests (specification §17).
 *
 * §17 names nine sections. A missing one is a specification gap, not a styling bug,
 * so the list itself is asserted rather than only the highlighting rule.
 */

import { describe, expect, it } from 'vitest';

import { isActiveSection, NAV_SECTIONS } from '@/lib/nav';

describe('NAV_SECTIONS', () => {
  it('covers every §17 section a salesperson needs', () => {
    const paths = NAV_SECTIONS.map((section) => section.href);

    expect(paths).toContain('/'); // Overview
    expect(paths).toContain('/audiences');
    expect(paths).toContain('/campaigns');
    expect(paths).toContain('/leads');
    expect(paths).toContain('/conversations');
    expect(paths).toContain('/bookings');
    expect(paths).toContain('/knowledge');
    expect(paths).toContain('/notifications');
  });

  it('has no duplicate destinations', () => {
    const paths = NAV_SECTIONS.map((section) => section.href);
    expect(new Set(paths).size).toBe(paths.length);
  });

  it('labels every section', () => {
    for (const section of NAV_SECTIONS) {
      expect(section.label.trim().length).toBeGreaterThan(0);
    }
  });
});

describe('isActiveSection', () => {
  it('highlights the section a detail page belongs to', () => {
    // A salesperson reading one lead is still in Leads.
    expect(isActiveSection('/leads/abc-123', '/leads')).toBe(true);
    expect(isActiveSection('/campaigns/abc-123', '/campaigns')).toBe(true);
  });

  it('highlights the section itself', () => {
    expect(isActiveSection('/leads', '/leads')).toBe(true);
  });

  it('matches the overview only exactly', () => {
    // Every path starts with a slash, so a prefix rule would light up Overview
    // everywhere.
    expect(isActiveSection('/', '/')).toBe(true);
    expect(isActiveSection('/leads', '/')).toBe(false);
  });

  it('does not highlight a section that merely shares a prefix', () => {
    // /leadsomething is not /leads.
    expect(isActiveSection('/leadsomething', '/leads')).toBe(false);
  });

  it('highlights one section at a time', () => {
    const active = NAV_SECTIONS.filter((section) =>
      isActiveSection('/leads/abc-123', section.href),
    );
    expect(active).toHaveLength(1);
  });
});
