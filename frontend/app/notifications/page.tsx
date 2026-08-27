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

import { useCallback, useEffect, useState } from 'react';

import { AppShell } from '@/components/shell';
import { Alert, Button, EmptyState, PageHeader, Skeleton } from '@/components/ui';
import { api, type Notification } from '@/lib/api';
import { cx } from '@/lib/cx';

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
    <AppShell>
      <PageHeader
        title="Notifications"
        description={unread > 0 ? `${unread} unread` : 'Nothing unread'}
        actions={
          unread > 0 ? (
            <Button type="button" onClick={markAllRead}>
              Mark all read
            </Button>
          ) : null
        }
      />

      {error ? <Alert className="mb-6">{error}</Alert> : null}

      {loading ? (
        <Skeleton className="h-48" />
      ) : items.length === 0 ? (
        <EmptyState
          title="No notifications yet"
          description="A booked demo notifies everyone on the team here and on the shared calendar — nowhere else."
        />
      ) : (
        <ul className="space-y-3">
          {items.map((notification) => (
            <li
              key={notification.id}
              className={cx(
                'rounded-2xl border p-5 shadow-card',
                notification.is_read ? 'border-line bg-white' : 'border-forest/25 bg-forest-faint',
              )}
            >
              <div className="flex items-start justify-between gap-4">
                <div>
                  {!notification.is_read ? (
                    <p className="mb-1 text-[11px] font-semibold uppercase tracking-section text-forest">
                      Unread
                    </p>
                  ) : null}
                  <p className="font-medium">{notification.title}</p>
                  <p className="mt-0.5 text-sm text-ink-muted">{notification.message}</p>
                  <p className="mt-2 text-xs text-ink-faint">
                    {formatWhen(notification.created_at)}
                  </p>
                </div>
                {notification.is_read ? null : (
                  <button
                    type="button"
                    onClick={() => markRead(notification.id)}
                    className="shrink-0 text-sm font-medium text-ink-muted hover:text-ink"
                  >
                    Mark read
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </AppShell>
  );
}
