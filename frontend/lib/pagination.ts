/**
 * Pagination arithmetic (§17, D-35).
 *
 * The list APIs have always paged correctly and returned totals; the dashboard asked
 * for fifty rows and rendered them with no way to reach the rest. Past fifty leads the
 * list silently showed a subset — the kind of thing a salesperson discovers by
 * concluding a lead was lost.
 *
 * Kept as pure functions so the edge cases can be tested without rendering: an empty
 * list, a total that is an exact multiple of the page size, and a stale offset left
 * pointing past the end after rows were filtered away.
 */

export const DEFAULT_PAGE_SIZE = 50;

export interface PageState {
  total: number;
  limit: number;
  offset: number;
}

/** How many pages there are. Zero rows is one (empty) page, not zero. */
export function pageCount(total: number, limit: number): number {
  if (limit <= 0) {
    return 1;
  }
  return Math.max(Math.ceil(total / limit), 1);
}

/** Which page an offset falls on, counting from 1 for display. */
export function currentPage(offset: number, limit: number): number {
  if (limit <= 0) {
    return 1;
  }
  return Math.floor(Math.max(offset, 0) / limit) + 1;
}

export function hasPrevious(state: PageState): boolean {
  return state.offset > 0;
}

/**
 * Whether a next page exists.
 *
 * Derived from the total rather than from "did we get a full page", which is wrong
 * exactly when the total is a multiple of the page size — the case that produces an
 * empty final page.
 */
export function hasNext(state: PageState): boolean {
  return state.offset + state.limit < state.total;
}

/** The offset for the next page, clamped so it cannot run past the end. */
export function nextOffset(state: PageState): number {
  return hasNext(state) ? state.offset + state.limit : state.offset;
}

/** The offset for the previous page, clamped at zero. */
export function previousOffset(state: PageState): number {
  return Math.max(state.offset - state.limit, 0);
}

/**
 * Human-readable position, e.g. "Showing 51–100 of 214".
 *
 * The upper bound is clamped to the total so a partial final page does not claim rows
 * that are not there.
 */
export function rangeLabel(state: PageState, shown: number): string {
  if (state.total === 0) {
    return 'No results';
  }
  const first = state.offset + 1;
  const last = Math.min(state.offset + shown, state.total);
  return `Showing ${first}–${last} of ${state.total}`;
}

/**
 * An offset that is valid for this total.
 *
 * A filter change can leave the offset pointing past the end — the user is on page 5,
 * narrows the filter to twelve results, and would otherwise see an empty list with no
 * indication why. This pulls them back to the last page that has rows.
 */
export function clampOffset(offset: number, total: number, limit: number): number {
  if (offset <= 0 || total === 0 || limit <= 0) {
    return 0;
  }
  const lastPageOffset = (pageCount(total, limit) - 1) * limit;
  return Math.min(offset, lastPageOffset);
}
