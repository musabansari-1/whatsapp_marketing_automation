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

import { Nav } from '@/app/nav';
import { Pager } from '@/app/pager';
import { api, type Conversation, type ConversationStatus } from '@/lib/api';
import { clampOffset, DEFAULT_PAGE_SIZE } from '@/lib/pagination';

const STATUSES: ConversationStatus[] = ['ACTIVE', 'WAITING_FOR_USER', 'BOOKING', 'CLOSED'];

const STATUS_STYLES: Record<ConversationStatus, string> = {
  ACTIVE: 'bg-sky-100 text-sky-800',
  WAITING_FOR_USER: 'bg-slate-100 text-slate-700',
  BOOKING: 'bg-emerald-100 text-emerald-800',
  CLOSED: 'bg-slate-200 text-slate-600',
};

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
    <>
      <Nav />
      <main className="mx-auto max-w-4xl px-6 py-10">
        <h1 className="text-xl font-semibold tracking-tight">Conversations</h1>
        <p className="mt-1 text-sm text-slate-600">
          {total} {total === 1 ? 'conversation' : 'conversations'}, most recent first
        </p>

        <select
          value={status}
          onChange={(event) => {
            // Back to the first page: staying on page 5 of a shorter result set is
            // never what was meant.
            setOffset(0);
            setStatus(event.target.value as ConversationStatus | '');
          }}
          className="mt-5 rounded border border-slate-300 px-3 py-2 text-sm"
        >
          <option value="">All states</option>
          {STATUSES.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>

        {error ? (
          <p className="mt-6 rounded border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            {error}
          </p>
        ) : null}

        {loading ? (
          <p className="mt-6 text-sm text-slate-600">Loading…</p>
        ) : conversations.length === 0 ? (
          <p className="mt-6 rounded border border-slate-200 bg-white p-6 text-sm text-slate-600">
            No conversations yet. One starts when a customer replies.
          </p>
        ) : (
          <ul className="mt-6 space-y-2">
            {conversations.map((conversation) => (
              <li
                key={conversation.id}
                className="flex items-center justify-between rounded border border-slate-200 bg-white p-4"
              >
                <div>
                  <Link href={`/leads/${conversation.lead_id}`} className="text-sm underline">
                    Open lead
                  </Link>
                  <p className="mt-1 text-xs text-slate-500">
                    Last activity {lastActivity(conversation)}
                  </p>
                </div>
                <span
                  className={`rounded px-2 py-1 text-xs font-medium ${
                    STATUS_STYLES[conversation.status]
                  }`}
                >
                  {conversation.status}
                </span>
              </li>
            ))}
          </ul>
        )}

        {conversations.length > 0 ? (
          <Pager
            state={{ total, limit: DEFAULT_PAGE_SIZE, offset }}
            shown={conversations.length}
            onChange={setOffset}
          />
        ) : null}
      </main>
    </>
  );
}
