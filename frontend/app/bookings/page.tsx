/**
 * Booked demos (specification §12, §17).
 *
 * Read-only. §12 puts booking behind the availability re-check and Calendar
 * confirmation, so there is deliberately no way to create one from here — a booking
 * row written by hand would be a demo the sales calendar knows nothing about.
 */

'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';

import { api, type Booking } from '@/lib/api';

function formatSlot(booking: Booking): string {
  // Rendered in the booking's own timezone ([D8]): the sales team reads this, and a
  // browser-local time would be wrong for anyone travelling.
  return new Date(booking.start_time).toLocaleString('en-GB', {
    dateStyle: 'full',
    timeStyle: 'short',
    timeZone: booking.timezone,
  });
}

const STATUS_STYLES: Record<Booking['status'], string> = {
  CONFIRMED: 'bg-emerald-100 text-emerald-800',
  PENDING: 'bg-amber-100 text-amber-800',
  CANCELLED: 'bg-slate-200 text-slate-700',
  FAILED: 'bg-red-100 text-red-800',
};

export default function BookingsPage() {
  const [bookings, setBookings] = useState<Booking[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .listBookings()
      .then((page) => setBookings(page.items))
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  return (
    <main className="mx-auto max-w-4xl px-6 py-12">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Demos</h1>
          <p className="mt-1 text-sm text-slate-600">
            Booked by the assistant and confirmed by Google Calendar.
          </p>
        </div>
        <Link href="/" className="text-sm text-slate-600 underline">
          Back
        </Link>
      </div>

      {error ? (
        <p className="mt-6 rounded border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          {error}
        </p>
      ) : null}

      {loading ? (
        <p className="mt-6 text-sm text-slate-600">Loading…</p>
      ) : bookings.length === 0 ? (
        <p className="mt-6 rounded border border-slate-200 bg-white p-6 text-sm text-slate-600">
          No demos booked yet.
        </p>
      ) : (
        <ul className="mt-6 space-y-3">
          {bookings.map((booking) => (
            <li key={booking.id} className="rounded border border-slate-200 bg-white p-4">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <p className="font-medium">{formatSlot(booking)}</p>
                  <p className="text-sm text-slate-600">{booking.timezone}</p>
                  {booking.customer_email ? (
                    <p className="mt-1 text-sm text-slate-600">{booking.customer_email}</p>
                  ) : null}
                  {booking.failure_reason ? (
                    <p className="mt-1 text-sm text-red-700">{booking.failure_reason}</p>
                  ) : null}
                </div>
                <span
                  className={`shrink-0 rounded px-2 py-1 text-xs font-medium ${
                    STATUS_STYLES[booking.status]
                  }`}
                >
                  {booking.status}
                </span>
              </div>
              {booking.meeting_link ? (
                <a
                  href={booking.meeting_link}
                  className="mt-2 inline-block text-sm text-slate-700 underline"
                  target="_blank"
                  rel="noreferrer"
                >
                  Open in calendar
                </a>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
