/**
 * Conversations, for the active product (specification §17, §28.5).
 *
 * Ordered by most recent activity, because the question a salesperson is asking is
 * "what has happened since I last looked?", not "what exists?".
 *
 * Each conversation's product and segment were fixed when it began and are only shown
 * here: switching product or segment filter in the dashboard never changes the context a
 * conversation runs under.
 *
 * Read-only. Replies come from the worker, which is where the 24-hour window and
 * suppression checks live — a send button here would be a way around both.
 */

'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';

import { Pager } from '@/app/pager';
import { useProduct } from '@/components/product-context';
import { SegmentBadge, SegmentFilter } from '@/components/segment-filter';
import { StatusBadge } from '@/components/status-badge';
import { Alert, EmptyState, PageHeader, Skeleton } from '@/components/ui';
import type { Conversation, ConversationStatus } from '@/lib/api';
import { clampOffset, DEFAULT_PAGE_SIZE } from '@/lib/pagination';
import { productHref } from '@/lib/product-routes';

const STATUSES: ConversationStatus[] = ['ACTIVE', 'WAITING_FOR_USER', 'BOOKING', 'CLOSED'];

function lastActivity(conversation: Conversation): string {
  const stamps = [conversation.last_inbound_at, conversation.last_outbound_at].filter(
    (value): value is string => value !== null,
  );
  if (stamps.length === 0) {
    return new Date(conversation.created_at).toLocaleString('en-GB', { dateStyle: 'medium' });
  }
  const latest = stamps.sort().at(-1) as string;
  return new Date(latest).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' });
}

export default function ConversationsPage() {
  const { api, slug, product } = useProduct();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState<ConversationStatus | ''>('');
  const [segment, setSegment] = useState('');
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    api
      .listConversations({
        status: status || undefined,
        segment: segment || undefined,
        limit: DEFAULT_PAGE_SIZE,
        offset,
      })
      .then((page) => {
        setConversations(page.items);
        setTotal(page.total);
        const corrected = clampOffset(offset, page.total, DEFAULT_PAGE_SIZE);
        if (corrected !== offset) {
          setOffset(corrected);
        }
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [api, status, segment, offset]);

  useEffect(load, [load]);

  return (
    <>
      <PageHeader
        title="Conversations"
        description={`${total} ${total === 1 ? 'conversation' : 'conversations'} with ${product.display_name}, most recent first. Read-only — replies are sent by the worker.`}
      />

      <div className="mb-6 flex flex-wrap gap-3">
        <select
          aria-label="State"
          value={status}
          onChange={(event) => {
            // Back to the first page: staying on page 5 of a shorter result set is
            // never what was meant.
            setOffset(0);
            setStatus(event.target.value as ConversationStatus | '');
          }}
          className="ui-select w-full max-w-[16rem]"
        >
          <option value="">All states</option>
          {STATUSES.map((option) => (
            <option key={option} value={option}>
              {option.replaceAll('_', ' ')}
            </option>
          ))}
        </select>
        <SegmentFilter
          value={segment}
          onChange={(value) => {
            setOffset(0);
            setSegment(value);
          }}
        />
      </div>

      {error ? <Alert className="mb-6">{error}</Alert> : null}

      {loading ? (
        <Skeleton className="h-48" />
      ) : conversations.length === 0 ? (
        <EmptyState
          title="No conversations here"
          description="One starts when a customer replies to this product's number. There is no send button here: the worker owns the 24-hour window."
        />
      ) : (
        <>
          <ul className="space-y-2">
            {conversations.map((conversation) => (
              <li key={conversation.id}>
                <Link
                  href={productHref(slug, `/leads/${conversation.lead_id}`)}
                  className="flex items-center justify-between gap-3 rounded-2xl border border-line bg-white p-4 shadow-card transition hover:border-ink/15 hover:shadow-lift"
                >
                  <div className="min-w-0">
                    <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-ink">
                      Open lead
                      <SegmentBadge segmentId={conversation.segment_id} />
                    </p>
                    <p className="mt-1 text-xs text-ink-muted">
                      Last activity {lastActivity(conversation)}
                    </p>
                  </div>
                  <StatusBadge status={conversation.status} />
                </Link>
              </li>
            ))}
          </ul>
          <Pager
            state={{ total, limit: DEFAULT_PAGE_SIZE, offset }}
            shown={conversations.length}
            onChange={setOffset}
          />
        </>
      )}
    </>
  );
}
