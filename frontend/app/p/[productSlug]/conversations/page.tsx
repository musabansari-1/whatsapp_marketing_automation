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
 * Each row names the person and shows the latest message, and marks the ones where the
 * customer wrote last. Replying happens on the lead page, where the 24-hour window and
 * opt-outs are checked before anything is queued.
 */

'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';

import { Pager } from '@/app/pager';
import { useProduct } from '@/components/product-context';
import { SegmentBadge, SegmentFilter } from '@/components/segment-filter';
import { StatusBadge } from '@/components/status-badge';
import { Alert, EmptyState, PageHeader, Skeleton } from '@/components/ui';
import type { ConversationListItem, ConversationStatus } from '@/lib/api';
import { cx } from '@/lib/cx';
import { formatPhone, humanizeStatus, shortWhen, splitLinks } from '@/lib/format';
import { clampOffset, DEFAULT_PAGE_SIZE } from '@/lib/pagination';
import { productHref } from '@/lib/product-routes';

const STATUSES: ConversationStatus[] = ['ACTIVE', 'WAITING_FOR_USER', 'BOOKING', 'CLOSED'];

function lastActivity(conversation: ConversationListItem): string {
  const stamps = [
    conversation.last_message_at,
    conversation.last_inbound_at,
    conversation.last_outbound_at,
  ].filter((value): value is string => value !== null);
  return shortWhen(stamps.sort().at(-1) ?? conversation.created_at);
}

/** The latest message as one line, with links shown by their short label. */
function preview(text: string): string {
  return splitLinks(text)
    .map((part) => (part.kind === 'text' ? part.text : part.label))
    .join('');
}

function initials(name: string | null): string {
  if (!name) return '#';
  const words = name
    .replace(/^Dr\.?\s+/i, '')
    .split(/\s+/)
    .filter(Boolean);
  return ((words[0]?.charAt(0) ?? '') + (words[1]?.charAt(0) ?? '')).toUpperCase() || '#';
}

export default function ConversationsPage() {
  const { api, slug, product } = useProduct();
  const [conversations, setConversations] = useState<ConversationListItem[]>([]);
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
        description={`${total} ${total === 1 ? 'conversation' : 'conversations'} with ${product.display_name}, most recent first. Open one to read the chat or reply as the team.`}
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
              {humanizeStatus(option)}
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
          description="One starts when a customer messages or replies to this product's WhatsApp number."
        />
      ) : (
        <>
          <ul className="divide-y divide-line overflow-hidden rounded-2xl border border-line bg-white shadow-card">
            {conversations.map((conversation) => {
              const name = conversation.lead_name;
              const phone = formatPhone(conversation.lead_phone);
              return (
                <li key={conversation.id}>
                  <Link
                    href={productHref(slug, `/leads/${conversation.lead_id}`)}
                    className="flex items-start gap-3 px-4 py-3.5 transition hover:bg-canvas"
                  >
                    <span
                      aria-hidden="true"
                      className="flex size-10 shrink-0 items-center justify-center rounded-full bg-forest-soft text-xs font-bold text-forest"
                    >
                      {initials(name)}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline justify-between gap-3">
                        <p className="truncate text-sm font-semibold text-ink">
                          {name ?? phone ?? 'Unknown customer'}
                          {name && phone ? (
                            <span className="ml-2 font-normal text-ink-muted">{phone}</span>
                          ) : null}
                        </p>
                        <span
                          className={cx(
                            'shrink-0 text-xs',
                            conversation.awaiting_reply
                              ? 'font-semibold text-forest'
                              : 'text-ink-muted',
                          )}
                        >
                          {lastActivity(conversation)}
                        </span>
                      </div>
                      <div className="mt-1 flex items-center justify-between gap-3">
                        <p
                          className={cx(
                            'truncate text-sm',
                            conversation.awaiting_reply ? 'text-ink' : 'text-ink-muted',
                          )}
                        >
                          {conversation.last_message === null ? (
                            <span className="italic">No messages yet</span>
                          ) : (
                            <>
                              {conversation.last_message_direction === 'OUTBOUND' ? (
                                <span className="text-ink-faint">You: </span>
                              ) : null}
                              {preview(conversation.last_message)}
                            </>
                          )}
                        </p>
                        {conversation.awaiting_reply ? (
                          <span className="shrink-0 rounded-full bg-forest px-2 py-0.5 text-[10px] font-bold text-white">
                            Waiting for reply
                          </span>
                        ) : null}
                      </div>
                      <div className="mt-1.5 flex flex-wrap items-center gap-2">
                        <SegmentBadge segmentId={conversation.segment_id} />
                        {conversation.status !== 'ACTIVE' ? (
                          <StatusBadge status={conversation.status} />
                        ) : null}
                      </div>
                    </div>
                  </Link>
                </li>
              );
            })}
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
