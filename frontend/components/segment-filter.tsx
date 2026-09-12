/**
 * Segment filter and label (§28, [D49]).
 *
 * A segment is a filter inside a product, not a second boundary: "All segments" is always
 * the default, and a product with no segments shows no filter at all rather than a
 * control with nothing to choose.
 *
 * Filters by slug, which is what the API accepts and what makes a filtered page shareable.
 */

'use client';

import { useProduct } from '@/components/product-context';
import { cx } from '@/lib/cx';

export function SegmentFilter({
  value,
  onChange,
  className,
}: {
  value: string;
  onChange: (segmentSlug: string) => void;
  className?: string;
}) {
  const { product } = useProduct();
  if (product.segments.length === 0) {
    return null;
  }

  return (
    <select
      aria-label="Segment"
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className={cx('ui-select w-full max-w-[14rem]', className)}
    >
      <option value="">All segments</option>
      {product.segments.map((segment) => (
        <option key={segment.id} value={segment.slug}>
          {segment.display_name}
          {segment.is_active ? '' : ' (inactive)'}
        </option>
      ))}
    </select>
  );
}

/** The segment a row belongs to, or nothing when it has none. */
export function SegmentBadge({ segmentId }: { segmentId: string | null | undefined }) {
  const { segmentName } = useProduct();
  const name = segmentName(segmentId);
  if (!name) {
    return null;
  }
  return (
    <span className="inline-flex items-center whitespace-nowrap rounded-full bg-sky-50 px-2 py-0.5 text-[11px] font-semibold text-sky-800 ring-1 ring-sky-200/70">
      {name}
    </span>
  );
}
