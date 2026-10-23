/**
 * Notifications (specification §13, §17, [D3], [D47]).
 *
 * V1 notifies salespeople here and in the product's calendar, and nowhere else — no email,
 * no WhatsApp to the team. That is a scope rule, so this page is the whole notification
 * surface.
 *
 * Organisation-wide, because a person has one inbox, and every notification names its
 * product so a SustainX presentation is never mistaken for an Academicae demo. The
 * product filter narrows the list; the unread count does not change with it.
 */

'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';

import { IconBookings, IconLeads, IconNotifications } from '@/components/icons';
import { AppShell } from '@/components/shell';
import { Alert, Button, EmptyState, PageHeader, Skeleton } from '@/components/ui';
import { api, type Notification, type Product } from '@/lib/api';
import { cx } from '@/lib/cx';
import { productHref } from '@/lib/product-routes';

function formatWhen(value: string): string {
  return new Date(value).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' });
}

export default function NotificationsPage() {
  const [items, setItems] = useState<Notification[]>([]);
  const [unread, setUnread] = useState(0);
  const [products, setProducts] = useState<Product[]>([]);
  const [product, setProduct] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    api
      .listNotifications({ product: product || undefined })
      .then((page) => {
        setItems(page.items);
        setUnread(page.unread);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [product]);

  useEffect(load, [load]);

  useEffect(() => {
    api
      .listProducts()
      .then(setProducts)
      .catch(() => setProducts([]));
  }, []);

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
        description={
          loading
            ? 'Loading…'
            : unread > 0
              ? `${unread} unread across every product`
              : 'Nothing unread'
        }
        actions={
          unread > 0 ? (
            <Button type="button" onClick={markAllRead}>
              Mark all read
            </Button>
          ) : null
        }
      />

      {products.length > 1 ? (
        <select
          aria-label="Product"
          value={product}
          onChange={(event) => {
            setLoading(true);
            setProduct(event.target.value);
          }}
          className="ui-select mb-6 w-full max-w-[16rem]"
        >
          <option value="">All products</option>
          {products.map((option) => (
            <option key={option.id} value={option.slug}>
              {option.display_name}
            </option>
          ))}
        </select>
      ) : null}

      {error ? <Alert className="mb-6">{error}</Alert> : null}

      {loading ? (
        <Skeleton className="h-48" />
      ) : items.length === 0 ? (
        <EmptyState
          title="No notifications here"
          description="Hot leads (asked for a person, ready to register), booked demos and delivery problems show up here for the whole team."
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
              <div className="flex items-start gap-4">
                <NotificationIcon type={notification.type} />
                <div className="min-w-0 flex-1">
                  <div className="mb-1 flex flex-wrap items-center gap-2">
                    {notification.product ? (
                      <Link
                        href={productHref(notification.product.slug)}
                        className="rounded-lg bg-forest-soft px-2 py-0.5 text-[11px] font-bold text-forest"
                      >
                        {notification.product.display_name}
                      </Link>
                    ) : null}
                    {!notification.is_read ? (
                      <span className="text-[11px] font-semibold uppercase tracking-section text-forest">
                        Unread
                      </span>
                    ) : null}
                  </div>
                  <p className="font-medium">{notification.title}</p>
                  <p className="mt-0.5 text-sm text-ink-muted">{notification.message}</p>
                  <div className="mt-2 flex flex-wrap items-center gap-3 text-xs">
                    <span className="text-ink-faint">{formatWhen(notification.created_at)}</span>
                    {notification.product && notification.related_entity_type === 'booking' ? (
                      <Link
                        href={productHref(notification.product.slug, '/bookings')}
                        className="font-semibold text-ink-muted hover:text-ink"
                      >
                        Open demos
                      </Link>
                    ) : null}
                    {notification.product &&
                    notification.related_entity_type === 'lead' &&
                    notification.related_entity_id ? (
                      <Link
                        href={productHref(
                          notification.product.slug,
                          `/leads/${notification.related_entity_id}`,
                        )}
                        className="rounded-lg bg-forest px-2.5 py-1 font-semibold text-white transition hover:bg-forest/90"
                      >
                        Open lead
                      </Link>
                    ) : null}
                  </div>
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

const NOTIFICATION_ICONS = {
  LEAD_NEEDS_ATTENTION: { Icon: IconLeads, tone: 'bg-forest-soft text-forest' },
  DEMO_BOOKED: { Icon: IconBookings, tone: 'bg-sky-50 text-sky-700' },
  BOOKING_FAILED: { Icon: IconBookings, tone: 'bg-red-50 text-red-700' },
  BOT_SUSPECTED: { Icon: IconNotifications, tone: 'bg-amber-50 text-amber-800' },
} as const;

function NotificationIcon({ type }: { type: Notification['type'] }) {
  const { Icon, tone } = NOTIFICATION_ICONS[type] ?? NOTIFICATION_ICONS.BOT_SUSPECTED;
  return (
    <span
      className={cx('flex size-9 shrink-0 items-center justify-center rounded-xl', tone)}
      aria-hidden="true"
    >
      <Icon className="size-4" />
    </span>
  );
}
