/**
 * Booked demos, for the active product (specification §12, §17, [D43]).
 *
 * Each product books into its own calendar, so this lists this product's demos only.
 * Read-only: §12 puts booking behind the availability re-check and Calendar confirmation,
 * so there is deliberately no way to create one from here.
 */

'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';

import { Pager } from '@/app/pager';
import { useProduct } from '@/components/product-context';
import { SegmentFilter } from '@/components/segment-filter';
import { StatusBadge } from '@/components/status-badge';
import { Alert, EmptyState, PageHeader, Skeleton } from '@/components/ui';
import type { Booking } from '@/lib/api';
import { clampOffset, DEFAULT_PAGE_SIZE } from '@/lib/pagination';
import { productHref } from '@/lib/product-routes';

function formatSlot(booking: Booking): string {
  // Rendered in the booking's own timezone ([D8]): the sales team reads this, and a
  // browser-local time would be wrong for anyone travelling.
  return new Date(booking.start_time).toLocaleString('en-GB', {
    dateStyle: 'full',
    timeStyle: 'short',
    timeZone: booking.timezone,
  });
}

export default function BookingsPage() {
  const { api, slug, product } = useProduct();
  const [bookings, setBookings] = useState<Booking[]>([]);
  const [total, setTotal] = useState(0);
  const [segment, setSegment] = useState('');
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api
      .listBookings({ segment: segment || undefined, limit: DEFAULT_PAGE_SIZE, offset })
      .then((page) => {
        setBookings(page.items);
        setTotal(page.total);
        const corrected = clampOffset(offset, page.total, DEFAULT_PAGE_SIZE);
        if (corrected !== offset) {
          setOffset(corrected);
        }
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [api, segment, offset]);

  return (
    <>
      <PageHeader
        title="Demos"
        description={`Booked by the assistant and confirmed by ${product.display_name}'s calendar. There is no way to create one from here.`}
        actions={
          <SegmentFilter
            value={segment}
            onChange={(value) => {
              setOffset(0);
              setSegment(value);
            }}
          />
        }
      />

      {!product.has_calendar ? (
        <Alert tone="warning" className="mb-6">
          No calendar is configured for {product.display_name}, so the assistant cannot book demos
          for it. Nothing falls back to another product&apos;s calendar.
        </Alert>
      ) : null}

      {error ? <Alert className="mb-6">{error}</Alert> : null}

      {loading ? (
        <Skeleton className="h-48" />
      ) : bookings.length === 0 ? (
        <EmptyState
          title="No demos booked"
          description="A demo appears here only after Calendar confirms the event. A zero is empty pipeline, not a missing feature."
        />
      ) : (
        <>
          <ul className="space-y-3">
            {bookings.map((booking) => (
              <li
                key={booking.id}
                className="rounded-2xl border border-line bg-white p-5 shadow-card"
              >
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <p className="font-medium tracking-tight">{formatSlot(booking)}</p>
                    <p className="mt-0.5 text-sm text-ink-muted">{booking.timezone}</p>
                    {booking.customer_email ? (
                      <p className="mt-1 text-sm text-ink-muted">{booking.customer_email}</p>
                    ) : null}
                    {booking.failure_reason ? (
                      <p className="mt-1 text-sm text-red-800">{booking.failure_reason}</p>
                    ) : null}
                  </div>
                  <StatusBadge status={booking.status} />
                </div>
                <div className="mt-3 flex flex-wrap gap-4 text-sm">
                  <Link
                    href={productHref(slug, `/leads/${booking.lead_id}`)}
                    className="font-medium text-ink hover:text-forest"
                  >
                    Open lead
                  </Link>
                  {booking.meeting_link ? (
                    <a
                      href={booking.meeting_link}
                      className="font-medium text-ink-muted hover:text-ink"
                      target="_blank"
                      rel="noreferrer"
                    >
                      {booking.meeting_link.includes("meet.google.com")
                        ? "Join Meet"
                        : "Open in calendar"}
                    </a>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
          <Pager
            state={{ total, limit: DEFAULT_PAGE_SIZE, offset }}
            shown={bookings.length}
            onChange={setOffset}
          />
        </>
      )}
    </>
  );
}
