/**
 * Dashboard navigation tests (specification §17, §28).
 *
 * §17 names the sections a salesperson needs. A missing one is a specification gap, not
 * a styling bug, so the lists themselves are asserted rather than only the highlighting
 * rule.
 */

import { describe, expect, it } from 'vitest';

import { GLOBAL_SECTIONS, isActiveSection, PRODUCT_SECTIONS, sectionFor } from '@/lib/nav';

describe('PRODUCT_SECTIONS', () => {
  it('covers every §17 section, inside a product', () => {
    const keys = PRODUCT_SECTIONS.map((section) => section.key);

    for (const key of [
      'overview',
      'audiences',
      'campaigns',
      'templates',
      'leads',
      'conversations',
      'bookings',
      'knowledge',
    ]) {
      expect(keys).toContain(key);
    }
  });

  it('names no product', () => {
    // Products are data. A section path that mentioned one would be navigation that only
    // works for that product.
    for (const section of PRODUCT_SECTIONS) {
      expect(section.path.startsWith('/p/')).toBe(false);
    }
  });

  it('has no duplicate destinations', () => {
    const paths = PRODUCT_SECTIONS.map((section) => section.path);
    expect(new Set(paths).size).toBe(paths.length);
  });

  it('labels every section', () => {
    for (const section of [...PRODUCT_SECTIONS, ...GLOBAL_SECTIONS]) {
      expect(section.label.trim().length).toBeGreaterThan(0);
    }
  });
});

describe('GLOBAL_SECTIONS', () => {
  it('keeps notifications organisation-wide', () => {
    // A person has one inbox; each notification names its product instead.
    expect(GLOBAL_SECTIONS.map((section) => section.path)).toContain('/notifications');
  });

  it('has a way back to every product', () => {
    expect(GLOBAL_SECTIONS.map((section) => section.path)).toContain('/');
  });
});

describe('isActiveSection', () => {
  it('highlights the section a detail page belongs to', () => {
    expect(isActiveSection('/leads/abc-123', '/leads')).toBe(true);
    expect(isActiveSection('/campaigns/abc-123', '/campaigns')).toBe(true);
  });

  it('highlights the section itself', () => {
    expect(isActiveSection('/leads', '/leads')).toBe(true);
  });

  it('matches a product overview only exactly', () => {
    expect(isActiveSection('', '')).toBe(true);
    expect(isActiveSection('/leads', '')).toBe(false);
  });

  it('matches the global home only exactly', () => {
    expect(isActiveSection('/', '/')).toBe(true);
    expect(isActiveSection('/notifications', '/')).toBe(false);
  });

  it('does not highlight a section that merely shares a prefix', () => {
    expect(isActiveSection('/leadsomething', '/leads')).toBe(false);
  });

  it('highlights one product section at a time', () => {
    const active = PRODUCT_SECTIONS.filter((section) =>
      isActiveSection('/leads/abc-123', section.path),
    );
    expect(active).toHaveLength(1);
  });
});

describe('sectionFor', () => {
  it('finds the section a product path belongs to', () => {
    expect(sectionFor('/campaigns/new', PRODUCT_SECTIONS)?.key).toBe('campaigns');
    expect(sectionFor('', PRODUCT_SECTIONS)?.key).toBe('overview');
  });
});
