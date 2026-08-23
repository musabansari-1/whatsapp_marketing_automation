/**
 * One lead, with its conversation (specification §17).
 *
 * The §17 lead view in full: customer information, source, state, conversation,
 * booking and timestamps. This is where a salesperson picks up a thread the assistant
 * has been holding, so the transcript is the largest thing on the page.
 *
 * The AI's extracted facts are shown read-only. [D18] makes `qualification_data` the
 * model's target, and a human editing it would be arguing with the extraction rather
 * than correcting the record.
 */

'use client';

import Link from 'next/link';
import { use, useCallback, useEffect, useState } from 'react';

import { Nav } from '@/app/nav';
import { STATUS_STYLES } from '@/app/leads/status-styles';
import { api, type Booking, type ConversationDetail, type Lead } from '@/lib/api';

function formatWhen(value: string): string {
  return new Date(value).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' });
}

export default function LeadDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);

  const [lead, setLead] = useState<Lead | null>(null);
  const [conversation, setConversation] = useState<ConversationDetail | null>(null);
  const [bookings, setBookings] = useState<Booking[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    Promise.all([
      api.getLead(id),
      // A lead with no conversation yet is normal, not an error: it may have been
      // uploaded and never messaged.
      api.getLeadConversation(id).catch(() => null),
      api.listBookings().catch(() => null),
    ])
      .then(([leadResult, conversationResult, bookingPage]) => {
        setLead(leadResult);
        setConversation(conversationResult);
        setBookings((bookingPage?.items ?? []).filter((booking) => booking.lead_id === id));
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [id]);

  useEffect(load, [load]);

  if (loading) {
    return (
      <>
        <Nav />
        <main className="mx-auto max-w-4xl px-6 py-10 text-sm text-slate-600">Loading…</main>
      </>
    );
  }

  if (error || !lead) {
    return (
      <>
        <Nav />
        <main className="mx-auto max-w-4xl px-6 py-10">
          <p className="rounded border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            {error ?? 'Lead not found'}
          </p>
        </main>
      </>
    );
  }

  const facts = Object.entries(lead.qualification_data ?? {});

  return (
    <>
      <Nav />
      <main className="mx-auto max-w-4xl px-6 py-10">
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">
              {lead.full_name ?? lead.phone_e164}
            </h1>
            <p className="mt-1 text-sm text-slate-600">{lead.phone_e164}</p>
          </div>
          <div className="flex items-center gap-3">
            <span
              className={`rounded px-2 py-1 text-xs font-medium ${
                STATUS_STYLES[lead.status] ?? 'bg-slate-100 text-slate-700'
              }`}
            >
              {lead.status}
            </span>
            <Link href="/leads" className="text-sm text-slate-600 underline">
              Back
            </Link>
          </div>
        </div>

        {lead.opted_out ? (
          <p className="mt-4 rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">
            This customer has opted out. Nothing further will be sent to them.
          </p>
        ) : null}

        <section className="mt-6 grid gap-4 sm:grid-cols-2">
          <div className="rounded border border-slate-200 bg-white p-4">
            <h2 className="text-sm font-medium">Details</h2>
            <dl className="mt-2 space-y-1 text-sm">
              <Detail label="Email" value={lead.email} />
              <Detail label="Company" value={lead.company} />
              <Detail label="Role" value={lead.designation} />
              <Detail label="City" value={lead.city} />
              <Detail
                label="Source"
                value={lead.source_audience_id ? 'Audience upload' : 'Inbound message'}
              />
              <Detail label="Added" value={formatWhen(lead.created_at)} />
              <Detail
                label="Last contacted"
                value={lead.last_contacted_at ? formatWhen(lead.last_contacted_at) : null}
              />
            </dl>
          </div>

          <div className="rounded border border-slate-200 bg-white p-4">
            <h2 className="text-sm font-medium">What the assistant learned</h2>
            {facts.length === 0 ? (
              <p className="mt-2 text-sm text-slate-600">Nothing extracted yet.</p>
            ) : (
              <dl className="mt-2 space-y-1 text-sm">
                {facts.map(([key, value]) => (
                  <div key={key} className="flex gap-2">
                    <dt className="w-32 shrink-0 text-slate-500">{key}</dt>
                    <dd>{String(value)}</dd>
                  </div>
                ))}
              </dl>
            )}
          </div>
        </section>

        {bookings.length > 0 ? (
          <section className="mt-6 rounded border border-slate-200 bg-white p-4">
            <h2 className="text-sm font-medium">Demos</h2>
            <ul className="mt-2 space-y-1 text-sm">
              {bookings.map((booking) => (
                <li key={booking.id}>
                  {new Date(booking.start_time).toLocaleString('en-GB', {
                    dateStyle: 'full',
                    timeStyle: 'short',
                    // The booking's own timezone, not the browser's ([D8]).
                    timeZone: booking.timezone,
                  })}{' '}
                  <span className="text-slate-500">({booking.status})</span>
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        <section className="mt-6">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium">Conversation</h2>
            {conversation ? (
              <span className="text-xs text-slate-500">
                {conversation.conversation.status}
                {conversation.within_service_window
                  ? ' — can reply freely'
                  : ' — outside the 24-hour window'}
              </span>
            ) : null}
          </div>

          {!conversation ? (
            <p className="mt-2 rounded border border-slate-200 bg-white p-4 text-sm text-slate-600">
              No conversation yet.
            </p>
          ) : (
            <ul className="mt-2 space-y-2">
              {conversation.turns.map((turn) => (
                <li
                  key={turn.id}
                  className={`max-w-[80%] rounded p-3 text-sm ${
                    turn.role === 'user'
                      ? 'bg-white border border-slate-200'
                      : 'ml-auto bg-slate-900 text-white'
                  }`}
                >
                  <p className="whitespace-pre-wrap">{turn.content}</p>
                  <p
                    className={`mt-1 text-xs ${
                      turn.role === 'user' ? 'text-slate-500' : 'text-slate-300'
                    }`}
                  >
                    {formatWhen(turn.created_at)}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>
    </>
  );
}

function Detail({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="flex gap-2">
      <dt className="w-32 shrink-0 text-slate-500">{label}</dt>
      <dd>{value ?? '—'}</dd>
    </div>
  );
}
