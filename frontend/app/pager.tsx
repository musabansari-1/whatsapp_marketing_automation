/**
 * Pagination controls (§17, D-35).
 *
 * One component for every list, so the three pages cannot drift apart in behaviour or
 * appearance. The arithmetic lives in `lib/pagination.ts` where it can be tested
 * without rendering.
 *
 * Always shows the range, even on a single page. "Showing 1–12 of 12" tells a
 * salesperson the list is complete, which is the reassurance that was missing.
 */

'use client';

import {
  currentPage,
  hasNext,
  hasPrevious,
  nextOffset,
  pageCount,
  previousOffset,
  rangeLabel,
  type PageState,
} from '@/lib/pagination';

export function Pager({
  state,
  shown,
  onChange,
}: {
  state: PageState;
  /** Rows actually rendered, so a partial final page reports its real range. */
  shown: number;
  onChange: (offset: number) => void;
}) {
  const pages = pageCount(state.total, state.limit);

  return (
    <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-sm">
      <p className="text-ink-muted">{rangeLabel(state, shown)}</p>

      {pages > 1 ? (
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => onChange(previousOffset(state))}
            disabled={!hasPrevious(state)}
            className="rounded-lg border border-line bg-white px-3 py-1.5 text-ink transition hover:bg-canvas disabled:opacity-40"
          >
            Previous
          </button>
          <span className="tabular-nums text-ink-muted">
            Page {currentPage(state.offset, state.limit)} of {pages}
          </span>
          <button
            type="button"
            onClick={() => onChange(nextOffset(state))}
            disabled={!hasNext(state)}
            className="rounded-lg border border-line bg-white px-3 py-1.5 text-ink transition hover:bg-canvas disabled:opacity-40"
          >
            Next
          </button>
        </div>
      ) : null}
    </div>
  );
}
