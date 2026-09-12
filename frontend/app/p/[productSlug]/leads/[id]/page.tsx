/**
 * One lead, with its conversation (specification §17, §28).
 *
 * The §17 lead view in full: customer information, source, state, conversation, booking
 * and timestamps. This is where a salesperson picks up a thread the assistant has been
 * holding, so the transcript is the largest thing on the page.
 *
 * Everything here is this product's: the lead, its conversation and its demos. If the same
 * person is also a lead in another product, that is a different lead with its own page.
 *
 * The AI's extracted facts are shown read-only. [D18] makes `qualification_data` the
 * model's target, and a human editing it would be arguing with the extraction rather
 * than correcting the record.
 */

'use client';

import { useParams } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';

import { useProduct } from '@/components/product-context';
import { SegmentBadge } from '@/components/segment-filter';
import { StatusBadge } from '@/components/status-badge';
import { Alert, Card, PageHeader, Skeleton } from '@/components/ui';
import type { Booking, ConversationDetail, Lead } from '@/lib/api';
import { cx } from '@/lib/cx';
import { productHref } from '@/lib/product-routes';

function formatWhen(value: string): string {
  return new Date(value).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' });
}

export default function LeadDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { api, slug, product } = useProduct();

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
      api.listBookings({ leadId: id }).catch(() => null),
    ])
      .then(([leadResult, conversationResult, bookingPage]) => {
        setLead(leadResult);
        setConversation(conversationResult);
        setBookings(bookingPage?.items ?? []);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [api, id]);

  useEffect(load, [load]);

  if (loading) {
    return <Skeleton className="h-72" />;
  }

  if (error || !lead) {
    return <Alert>{error ?? 'Lead not found'}</Alert>;
  }

  const facts = Object.entries(lead.qualification_data ?? {});

  return (
    <>
      <PageHeader
        back={{ href: productHref(slug, '/leads'), label: 'Leads' }}
        title={lead.full_name ?? lead.phone_e164}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs">{lead.phone_e164}</span>
            <span className="text-xs">· {product.display_name} lead</span>
          </span>
        }
        actions={<StatusBadge status={lead.status} />}
      />

      {lead.opted_out ? (
        <Alert className="mb-6">
          This customer has opted out. Nothing further will be sent to them by any product.
        </Alert>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <div className="space-y-4">
          <Card>
            <h2 className="text-sm font-semibold">Details</h2>
            <dl className="mt-3 space-y-2 text-sm">
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
          </Card>

          <Card>
            <h2 className="text-sm font-semibold">What the assistant learned</h2>
            {facts.length === 0 ? (
              <p className="mt-2 text-sm text-ink-muted">Nothing extracted yet.</p>
            ) : (
              <dl className="mt-3 space-y-2 text-sm">
                {facts.map(([key, value]) => (
                  <div key={key} className="flex gap-2">
                    <dt className="w-32 shrink-0 text-ink-muted">{key}</dt>
                    <dd>{String(value)}</dd>
                  </div>
                ))}
              </dl>
            )}
          </Card>

          {bookings.length > 0 ? (
            <Card>
              <h2 className="text-sm font-semibold">Demos</h2>
              <ul className="mt-3 space-y-2 text-sm">
                {bookings.map((booking) => (
                  <li key={booking.id} className="flex items-start justify-between gap-3">
                    <span>
                      {new Date(booking.start_time).toLocaleString('en-GB', {
                        dateStyle: 'full',
                        timeStyle: 'short',
                        // The booking's own timezone, not the browser's ([D8]).
                        timeZone: booking.timezone,
                      })}
                    </span>
                    <StatusBadge status={booking.status} />
                  </li>
                ))}
              </ul>
            </Card>
          ) : null}
        </div>

        <section>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <h2 className="flex items-center gap-2 text-sm font-semibold">
              Conversation
              {conversation ? (
                <SegmentBadge segmentId={conversation.conversation.segment_id} />
              ) : null}
            </h2>
            {conversation ? (
              <span className="text-xs text-ink-muted">
                {conversation.conversation.status}
                {conversation.within_service_window
                  ? ' — can reply freely'
                  : ' — outside the 24-hour window'}
              </span>
            ) : null}
          </div>

          {!conversation ? (
            <Card>
              <p className="text-sm text-ink-muted">No conversation yet.</p>
            </Card>
          ) : (
            <div className="space-y-3 rounded-2xl border border-line bg-white p-4 shadow-card">
              {conversation.turns.map((turn) => (
                <div
                  key={turn.id}
                  className={cx(
                    'max-w-[85%] rounded-2xl px-4 py-3 text-sm leading-relaxed',
                    turn.role === 'user'
                      ? 'border border-line bg-canvas text-ink'
                      : 'ml-auto bg-ink text-white',
                  )}
                >
                  <p className="whitespace-pre-wrap">{turn.content}</p>
                  <p
                    className={cx(
                      'mt-1.5 text-[11px]',
                      turn.role === 'user' ? 'text-ink-muted' : 'text-white/55',
                    )}
                  >
                    {formatWhen(turn.created_at)}
                  </p>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </>
  );
}

function Detail({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="flex gap-2">
      <dt className="w-32 shrink-0 text-ink-muted">{label}</dt>
      <dd>{value ?? '—'}</dd>
    </div>
  );
}
