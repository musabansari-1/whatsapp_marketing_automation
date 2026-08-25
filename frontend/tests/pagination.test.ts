/**
 * Pagination tests (§17, D-35).
 *
 * The edge cases are the point: an exact multiple of the page size, a stale offset left
 * past the end by a filter change, and an empty list. Each is a way to show a
 * salesperson something wrong.
 */

import { describe, expect, it } from 'vitest';

import {
  clampOffset,
  currentPage,
  hasNext,
  hasPrevious,
  nextOffset,
  pageCount,
  previousOffset,
  rangeLabel,
} from '@/lib/pagination';

describe('pageCount', () => {
  it('counts a partial final page', () => {
    expect(pageCount(214, 50)).toBe(5);
  });

  it('does not add an empty page on an exact multiple', () => {
    // 100 rows at 50 a page is two pages, not three.
    expect(pageCount(100, 50)).toBe(2);
  });

  it('treats an empty list as one page', () => {
    // Zero pages would render "Page 1 of 0".
    expect(pageCount(0, 50)).toBe(1);
  });
});

describe('currentPage', () => {
  it('counts from one for display', () => {
    expect(currentPage(0, 50)).toBe(1);
    expect(currentPage(50, 50)).toBe(2);
    expect(currentPage(200, 50)).toBe(5);
  });

  it('survives a negative offset', () => {
    expect(currentPage(-10, 50)).toBe(1);
  });
});

describe('hasNext', () => {
  it('is true while rows remain', () => {
    expect(hasNext({ total: 214, limit: 50, offset: 0 })).toBe(true);
    expect(hasNext({ total: 214, limit: 50, offset: 150 })).toBe(true);
  });

  it('is false on the final page', () => {
    expect(hasNext({ total: 214, limit: 50, offset: 200 })).toBe(false);
  });

  it('is false when the total is an exact multiple', () => {
    // The case a "did we get a full page?" check gets wrong, offering an empty page.
    expect(hasNext({ total: 100, limit: 50, offset: 50 })).toBe(false);
  });

  it('is false for an empty list', () => {
    expect(hasNext({ total: 0, limit: 50, offset: 0 })).toBe(false);
  });
});

describe('hasPrevious', () => {
  it('is false on the first page', () => {
    expect(hasPrevious({ total: 214, limit: 50, offset: 0 })).toBe(false);
  });

  it('is true anywhere else', () => {
    expect(hasPrevious({ total: 214, limit: 50, offset: 50 })).toBe(true);
  });
});

describe('offsets', () => {
  it('advances by one page', () => {
    expect(nextOffset({ total: 214, limit: 50, offset: 50 })).toBe(100);
  });

  it('does not advance past the end', () => {
    expect(nextOffset({ total: 214, limit: 50, offset: 200 })).toBe(200);
  });

  it('goes back by one page', () => {
    expect(previousOffset({ total: 214, limit: 50, offset: 100 })).toBe(50);
  });

  it('does not go below zero', () => {
    expect(previousOffset({ total: 214, limit: 50, offset: 20 })).toBe(0);
  });
});

describe('rangeLabel', () => {
  it('describes the first page', () => {
    expect(rangeLabel({ total: 214, limit: 50, offset: 0 }, 50)).toBe('Showing 1–50 of 214');
  });

  it('describes a middle page', () => {
    expect(rangeLabel({ total: 214, limit: 50, offset: 50 }, 50)).toBe('Showing 51–100 of 214');
  });

  it('does not claim rows a partial page does not have', () => {
    expect(rangeLabel({ total: 214, limit: 50, offset: 200 }, 14)).toBe('Showing 201–214 of 214');
  });

  it('says so when there is nothing', () => {
    expect(rangeLabel({ total: 0, limit: 50, offset: 0 }, 0)).toBe('No results');
  });

  it('reassures on a single complete page', () => {
    // "Showing 1–12 of 12" is the confirmation that the list is complete — which is
    // exactly what was missing before.
    expect(rangeLabel({ total: 12, limit: 50, offset: 0 }, 12)).toBe('Showing 1–12 of 12');
  });
});

describe('clampOffset', () => {
  it('leaves a valid offset alone', () => {
    expect(clampOffset(100, 214, 50)).toBe(100);
  });

  it('pulls a stale offset back to the last page with rows', () => {
    // The user is on page 5, narrows the filter to twelve results, and would otherwise
    // see an empty list with no indication why.
    expect(clampOffset(200, 12, 50)).toBe(0);
  });

  it('returns to the first page when everything is filtered away', () => {
    expect(clampOffset(200, 0, 50)).toBe(0);
  });

  it('never returns a negative offset', () => {
    expect(clampOffset(-50, 214, 50)).toBe(0);
  });
});
