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
 *
 * A team member can reply into the chat while WhatsApp's 24-hour window is open. The
 * reply pauses the assistant for a while so it does not answer over them; "Resume
 * assistant" hands the chat back.
 */

'use client';

import { useParams } from 'next/navigation';
import { type FormEvent, useCallback, useEffect, useState } from 'react';

import { useProduct } from '@/components/product-context';
import { SegmentBadge } from '@/components/segment-filter';
import { StatusBadge } from '@/components/status-badge';
import { Alert, Button, Card, PageHeader, Skeleton } from '@/components/ui';
import type { Booking, ConversationDetail, ConversationTurn, Lead } from '@/lib/api';
import { cx } from '@/lib/cx';
import {
  dayLabel,
  factLabel,
  factValue,
  formatPhone,
  humanizeStatus,
  replyWindowLeft,
  splitLinks,
  whatsappUrl,
} from '@/lib/format';
import { productHref } from '@/lib/product-routes';

function formatWhen(value: string): string {
  return new Date(value).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' });
}

function sentByTeam(turn: ConversationTurn): boolean {
  return turn.tool_calls?.sent_by === 'team';
}

function isPaused(until: string | null | undefined): boolean {
  return Boolean(until && new Date(until).getTime() > Date.now());
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
            <span className="text-sm">{formatPhone(lead.phone_e164)}</span>
            <span className="text-xs">· {product.display_name} lead</span>
          </span>
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={lead.status} />
            <a
              href={whatsappUrl(lead.phone_e164)}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-xl border border-line bg-white px-3 py-1.5 text-xs font-semibold text-ink shadow-input transition hover:border-forest/40 hover:text-forest"
            >
              Open in WhatsApp
            </a>
            <CopyButton text={lead.phone_e164} />
          </div>
        }
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
              <Detail label="Phone" value={formatPhone(lead.phone_e164)} />
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
                    <dt className="w-40 shrink-0 text-ink-muted">{factLabel(key)}</dt>
                    <dd className="min-w-0 break-words font-medium">{factValue(value)}</dd>
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
            {conversation ? <WindowStatus conversation={conversation} /> : null}
          </div>

          {!conversation ? (
            <Card>
              <p className="text-sm text-ink-muted">No conversation yet.</p>
            </Card>
          ) : (
            <Transcript turns={conversation.turns} />
          )}

          {conversation && conversation.conversation.status !== 'CLOSED' ? (
            <TeamReply
              leadId={lead.id}
              conversation={conversation}
              optedOut={lead.opted_out}
              onChange={load}
            />
          ) : null}
        </section>
      </div>
    </>
  );
}

/** A detail row; empty details are left out rather than shown as a dash. */
function Detail({ label, value }: { label: string; value: string | null }) {
  if (!value) return null;
  return (
    <div className="flex gap-2">
      <dt className="w-32 shrink-0 text-ink-muted">{label}</dt>
      <dd className="min-w-0 break-words">{value}</dd>
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1500);
        });
      }}
      className="rounded-xl border border-line bg-white px-3 py-1.5 text-xs font-semibold text-ink-muted shadow-input transition hover:text-ink"
    >
      {copied ? 'Copied' : 'Copy number'}
    </button>
  );
}

/** How long the team can still reply freely, from the customer's last message. */
function WindowStatus({ conversation }: { conversation: ConversationDetail }) {
  const left = conversation.within_service_window
    ? replyWindowLeft(conversation.conversation.last_inbound_at)
    : null;
  return (
    <span className="text-xs text-ink-muted">
      {humanizeStatus(conversation.conversation.status)}
      {' · '}
      {left ? (
        <span className="font-semibold text-forest">Reply window closes in {left}</span>
      ) : conversation.within_service_window ? (
        'Reply window open'
      ) : (
        'Reply window closed: only an approved template can be sent'
      )}
    </span>
  );
}

/** Message text with links shortened and clickable; long words wrap inside the bubble. */
function MessageText({ text, outgoing }: { text: string; outgoing: boolean }) {
  return (
    <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
      {splitLinks(text).map((part, index) =>
        part.kind === 'text' ? (
          <span key={index}>{part.text}</span>
        ) : (
          <a
            key={index}
            href={part.href}
            target="_blank"
            rel="noopener noreferrer"
            title={part.href}
            className={cx(
              'font-semibold underline underline-offset-2',
              outgoing ? 'text-emerald-900' : 'text-forest',
            )}
          >
            {part.label}
          </a>
        ),
      )}
    </p>
  );
}

/** The chat as WhatsApp shows it: customer on the left, our replies on the right in
 *  green, with a divider for each day and who answered on every reply. */
function Transcript({ turns }: { turns: ConversationTurn[] }) {
  let previousDay = '';
  return (
    <div className="space-y-2 rounded-2xl border border-line bg-[#efeae2] p-3 shadow-card sm:p-4">
      {turns.map((turn) => {
        const day = dayLabel(turn.created_at);
        const showDay = day !== previousDay;
        previousDay = day;
        const outgoing = turn.role !== 'user';
        return (
          <div key={turn.id}>
            {showDay ? (
              <div className="my-2 flex justify-center">
                <span className="rounded-lg bg-white/90 px-2.5 py-1 text-[11px] font-medium text-ink-muted shadow-sm">
                  {day}
                </span>
              </div>
            ) : null}
            <div
              className={cx(
                'w-fit max-w-[85%] rounded-xl px-3 py-2 text-sm leading-relaxed shadow-sm',
                outgoing
                  ? 'ml-auto rounded-tr-sm bg-[#d9fdd3] text-ink'
                  : 'rounded-tl-sm bg-white text-ink',
              )}
            >
              {outgoing ? (
                <p className="mb-0.5 text-[11px] font-semibold text-forest">
                  {sentByTeam(turn) ? 'Team' : 'Assistant'}
                </p>
              ) : null}
              <MessageText text={turn.content} outgoing={outgoing} />
              <p className="mt-0.5 text-right text-[10px] text-ink-muted">
                {new Date(turn.created_at).toLocaleTimeString('en-GB', {
                  hour: '2-digit',
                  minute: '2-digit',
                })}
              </p>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function TeamReply({
  leadId,
  conversation,
  optedOut,
  onChange,
}: {
  leadId: string;
  conversation: ConversationDetail;
  optedOut: boolean;
  onChange: () => void;
}) {
  const { api } = useProduct();
  const [body, setBody] = useState('');
  const [pause, setPause] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const pausedUntil = conversation.conversation.ai_paused_until;
  const canReply = conversation.within_service_window && !optedOut;

  async function send(event: FormEvent) {
    event.preventDefault();
    if (!body.trim()) return;
    setBusy(true);
    setFailure(null);
    try {
      await api.replyToLead(leadId, body.trim(), pause ? 12 : 0);
      setBody('');
      setNotice('Sent. It appears in the conversation in a few seconds.');
      // The worker sends it; give it a moment, then refresh the transcript.
      window.setTimeout(onChange, 2500);
    } catch (e) {
      setFailure((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function resume() {
    setBusy(true);
    setFailure(null);
    try {
      await api.resumeAssistant(leadId);
      setNotice('The assistant will answer the next message.');
      onChange();
    } catch (e) {
      setFailure((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-4 space-y-3">
      {pausedUntil && isPaused(pausedUntil) ? (
        <Alert tone="warning" className="flex flex-wrap items-center justify-between gap-3">
          <span>
            The assistant is paused in this chat until {formatWhen(pausedUntil)}, so the team can
            answer.
          </span>
          <Button variant="secondary" onClick={resume} disabled={busy}>
            Resume assistant
          </Button>
        </Alert>
      ) : null}

      {canReply ? (
        <Card>
          <form onSubmit={send} className="space-y-3">
            <label htmlFor="team-reply" className="text-sm font-semibold">
              Reply as the team
            </label>
            <textarea
              id="team-reply"
              className="ui-input min-h-24 w-full"
              value={body}
              maxLength={4096}
              onChange={(e) => setBody(e.target.value)}
              placeholder="Type a reply. It is sent from this product's WhatsApp number."
            />
            <div className="flex flex-wrap items-center justify-between gap-3">
              <label className="flex items-center gap-2 text-xs text-ink-muted">
                <input
                  type="checkbox"
                  checked={pause}
                  onChange={(e) => setPause(e.target.checked)}
                />
                Pause the assistant for 12 hours
              </label>
              <Button type="submit" disabled={busy || !body.trim()}>
                Send reply
              </Button>
            </div>
          </form>
        </Card>
      ) : (
        <Card>
          <p className="text-sm text-ink-muted">
            {optedOut
              ? 'This customer opted out, so nothing can be sent.'
              : 'The customer last wrote over 24 hours ago, so WhatsApp only allows an approved template now.'}
          </p>
        </Card>
      )}

      {notice ? <Alert tone="success">{notice}</Alert> : null}
      {failure ? <Alert>{failure}</Alert> : null}
    </div>
  );
}
