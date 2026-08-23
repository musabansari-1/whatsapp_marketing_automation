/**
 * Notifications (specification §13, §17, [D3]).
 *
 * V1 notifies salespeople here and in the shared calendar, and nowhere else — no
 * email, no WhatsApp to the team. That is a scope rule, so this page is the whole
 * notification surface.
 *
 * Every request goes through the BFF proxy on the frontend origin ([D13]), which is
 * why the browser can call it at all: the session cookie is same-origin and HttpOnly.
 */

'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';

import { Nav } from '@/app/nav';
import { api, type Notification } from '@/lib/api';

function formatWhen(value: string): string {
  return new Date(value).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' });
}

export default function NotificationsPage() {
  const [items, setItems] = useState<Notification[]>([]);
  const [unread, setUnread] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    api
      .listNotifications()
      .then((page) => {
        setItems(page.items);
        setUnread(page.unread);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  async function markRead(id: string) {
    try {
      await api.markNotificationRead(id);
      // Reloaded rather than patched locally: the unread count drives the badge, and
      // guessing at it here would let the two drift apart.
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function markAllRead() {
    try {
      await api.markAllNotificationsRead();
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <>
      <Nav />
      <main className="mx-auto max-w-3xl px-6 py-12">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Notifications</h1>
            <p className="mt-1 text-sm text-slate-600">
              {unread > 0 ? `${unread} unread` : 'Nothing unread'}
            </p>
          </div>
          <div className="flex items-center gap-3">
            <Link href="/" className="text-sm text-slate-600 underline">
              Back
            </Link>
            {unread > 0 ? (
              <button
                type="button"
                onClick={markAllRead}
                className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white"
              >
                Mark all read
              </button>
            ) : null}
          </div>
        </div>

        {error ? (
          <p className="mt-6 rounded border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            {error}
          </p>
        ) : null}

        {loading ? (
          <p className="mt-6 text-sm text-slate-600">Loading…</p>
        ) : items.length === 0 ? (
          <p className="mt-6 rounded border border-slate-200 bg-white p-6 text-sm text-slate-600">
            No notifications yet. A booked demo notifies everyone on the team.
          </p>
        ) : (
          <ul className="mt-6 space-y-3">
            {items.map((notification) => (
              <li
                key={notification.id}
                className={`rounded border p-4 ${
                  notification.is_read
                    ? 'border-slate-200 bg-white'
                    : 'border-emerald-300 bg-emerald-50'
                }`}
              >
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <p className="font-medium">{notification.title}</p>
                    <p className="text-sm text-slate-700">{notification.message}</p>
                    <p className="mt-1 text-xs text-slate-500">
                      {formatWhen(notification.created_at)}
                    </p>
                  </div>
                  {notification.is_read ? null : (
                    <button
                      type="button"
                      onClick={() => markRead(notification.id)}
                      className="shrink-0 text-sm text-slate-600 underline"
                    >
                      Mark read
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </main>
    </>
  );
}
