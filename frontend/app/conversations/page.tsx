/**
 * Conversations (specification §17).
 *
 * Ordered by most recent activity, because the question a salesperson is asking is
 * "what has happened since I last looked?", not "what exists?".
 *
 * Read-only. Replies come from the worker, which is where the 24-hour window and
 * suppression checks live — a send button here would be a way around both.
 */

'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';

import { Pager } from '@/app/pager';
import { AppShell } from '@/components/shell';
import { StatusBadge } from '@/components/status-badge';
import { Alert, EmptyState, PageHeader, Skeleton } from '@/components/ui';
import { api, type Conversation, type ConversationStatus } from '@/lib/api';
import { clampOffset, DEFAULT_PAGE_SIZE } from '@/lib/pagination';

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
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState<ConversationStatus | ''>('');
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    api
      .listConversations(status || undefined, DEFAULT_PAGE_SIZE, offset)
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
  }, [status, offset]);

  useEffect(load, [load]);

  return (
    <AppShell>
      <PageHeader
        title="Conversations"
        description={`${total} ${total === 1 ? 'conversation' : 'conversations'}, most recent first. Read-only — replies are sent by the worker.`}
      />

      <select
        value={status}
        onChange={(event) => {
          // Back to the first page: staying on page 5 of a shorter result set is
          // never what was meant.
          setOffset(0);
          setStatus(event.target.value as ConversationStatus | '');
        }}
        className="ui-select mb-6 w-full max-w-[16rem]"
      >
        <option value="">All states</option>
        {STATUSES.map((option) => (
          <option key={option} value={option}>
            {option.replaceAll('_', ' ')}
          </option>
        ))}
      </select>

      {error ? <Alert className="mb-6">{error}</Alert> : null}

      {loading ? (
        <Skeleton className="h-48" />
      ) : conversations.length === 0 ? (
        <EmptyState
          title="No conversations yet"
          description="One starts when a customer replies. There is no send button here: the worker owns the 24-hour window."
        />
      ) : (
        <>
          <ul className="space-y-2">
            {conversations.map((conversation) => (
              <li key={conversation.id}>
                <Link
                  href={`/leads/${conversation.lead_id}`}
                  className="flex items-center justify-between rounded-2xl border border-line bg-white p-4 shadow-card transition hover:border-ink/15 hover:shadow-lift"
                >
                  <div>
                    <p className="text-sm font-medium text-ink">Open lead</p>
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
    </AppShell>
  );
}
