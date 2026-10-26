/**
 * Campaign detail and the Send Broadcast confirmation (specification §7, §28).
 *
 * Sending is the one destructive action in the platform, so it is behind an explicit
 * confirmation that states how many people will be messaged — and for which product, from
 * whose number, since that is the mistake a multi-product dashboard makes possible.
 */

'use client';

import { useParams } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';

import { Pager } from '@/app/pager';
import { useProduct } from '@/components/product-context';
import { SegmentBadge } from '@/components/segment-filter';
import { StatusBadge } from '@/components/status-badge';
import { Alert, Button, Card, FilterChip, PageHeader, Skeleton, StatCard } from '@/components/ui';
import type { CampaignDetail, CampaignRecipient, RecipientStatus } from '@/lib/api';
import {
  explainError,
  lastEvent,
  percent,
  STUCK_AFTER_MINUTES,
  stuckCount,
  summarize,
} from '@/lib/delivery';
import { formatPhone, shortWhen } from '@/lib/format';
import { DEFAULT_PAGE_SIZE } from '@/lib/pagination';
import { productHref } from '@/lib/product-routes';

const FILTERS: { label: string; value: RecipientStatus | 'ALL' }[] = [
  { label: 'All', value: 'ALL' },
  { label: 'Read', value: 'READ' },
  { label: 'Delivered', value: 'DELIVERED' },
  { label: 'Sent', value: 'SENT' },
  { label: 'Failed', value: 'FAILED' },
  { label: 'Queued', value: 'QUEUED' },
  { label: 'Pending', value: 'PENDING' },
  { label: 'Skipped', value: 'SKIPPED' },
];

// While a broadcast is going out, Meta's reports keep arriving; refresh to show them.
const LIVE_REFRESH_MS = 15_000;

export default function CampaignDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { api, slug, product } = useProduct();

  const [detail, setDetail] = useState<CampaignDetail | null>(null);
  const [recipients, setRecipients] = useState<CampaignRecipient[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState<RecipientStatus | 'ALL'>('ALL');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const load = useCallback(async () => {
    try {
      const [campaign, page] = await Promise.all([
        api.getCampaign(id),
        api.listCampaignRecipients(
          id,
          filter === 'ALL' ? undefined : filter,
          DEFAULT_PAGE_SIZE,
          offset,
        ),
      ]);
      setDetail(campaign);
      setRecipients(page.items);
      setTotal(page.total);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load this campaign.');
    }
  }, [api, id, filter, offset]);

  useEffect(() => {
    void load();
  }, [load]);

  const sending = detail?.campaign.status === 'PROCESSING';
  useEffect(() => {
    if (!sending) return;
    const timer = window.setInterval(() => void load(), LIVE_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [sending, load]);

  async function handleCancel() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api.cancelCampaign(id);
      setNotice('Campaign cancelled. Queued messages will not be sent.');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not cancel the campaign.');
    } finally {
      setBusy(false);
    }
  }

  async function handleSend() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.sendCampaign(id);
      setNotice(
        `Queued ${result.queued} message${result.queued === 1 ? '' : 's'}` +
          (result.skipped_as_suppressed > 0
            ? `. ${result.skipped_as_suppressed} skipped because they opted out.`
            : '.'),
      );
      setConfirming(false);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not start the broadcast.');
    } finally {
      setBusy(false);
    }
  }

  const campaign = detail?.campaign;
  // An older API has no live counts; its stored counters are the next best thing.
  const summary = summarize(
    detail?.status_counts ??
      (campaign
        ? {
            PENDING: detail?.pending_recipients ?? 0,
            SENT: campaign.sent_count,
            DELIVERED: campaign.delivered_count,
            READ: campaign.read_count,
            FAILED: campaign.failed_count,
            SKIPPED: campaign.skipped_count,
          }
        : {}),
  );
  const stuck = stuckCount(summary, campaign?.started_at ?? null);

  return (
    <>
      {error ? <Alert className="mb-4">{error}</Alert> : null}
      {notice ? (
        <Alert tone="success" className="mb-4">
          {notice}
        </Alert>
      ) : null}

      {!detail || !campaign ? (
        error ? null : (
          <Skeleton className="h-64" />
        )
      ) : (
        <>
          <PageHeader
            back={{ href: productHref(slug, '/campaigns'), label: 'Campaigns' }}
            title={campaign.name}
            description={
              <span className="flex flex-wrap items-center gap-2">
                <StatusBadge status={campaign.status} />
                <SegmentBadge segmentId={campaign.segment_id} />
                <span>template {detail.template.name}</span>
                {campaign.started_at ? (
                  <span>· started {shortWhen(campaign.started_at)}</span>
                ) : null}
                {campaign.completed_at ? (
                  <span>· finished {shortWhen(campaign.completed_at)}</span>
                ) : null}
              </span>
            }
          />

          {stuck > 0 ? (
            <Alert tone="warning" className="mb-4">
              {stuck} message{stuck === 1 ? ' has' : 's have'} been waiting to send for more than{' '}
              {STUCK_AFTER_MINUTES} minutes. They have most likely been lost by the send queue and
              will not go out on their own; ask the platform admin to re-queue them.
            </Alert>
          ) : null}

          <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <StatCard label="Recipients" value={summary.total} />
            <StatCard
              label="Delivered"
              value={summary.delivered}
              tone="text-forest"
              hint={
                [
                  percent(summary.delivered, summary.total) &&
                    `${percent(summary.delivered, summary.total)} of recipients`,
                  summary.awaitingReport > 0 && `+${summary.awaitingReport} sent, no report yet`,
                ]
                  .filter(Boolean)
                  .join(' · ') || undefined
              }
            />
            <StatCard
              label="Read"
              value={summary.read}
              tone="text-forest"
              hint={
                percent(summary.read, summary.delivered)
                  ? `${percent(summary.read, summary.delivered)} of delivered`
                  : undefined
              }
            />
            <StatCard
              label="Failed"
              value={summary.failed}
              tone={summary.failed > 0 ? 'text-red-700' : undefined}
              hint={
                percent(summary.failed, summary.total)
                  ? `${percent(summary.failed, summary.total)} of recipients`
                  : undefined
              }
            />
            <StatCard
              label="Not sent yet"
              value={summary.waiting}
              tone={stuck > 0 ? 'text-amber-800' : undefined}
              hint={sending ? 'Updates every 15 s' : undefined}
            />
            <StatCard
              label="Skipped"
              value={summary.skipped}
              tone="text-amber-800"
              hint="Opted out"
            />
          </div>

          {(detail.failure_reasons ?? []).length > 0 ? (
            <Card className="mt-4">
              <h2 className="text-[11px] font-semibold uppercase tracking-section text-ink-muted">
                Why messages failed
              </h2>
              <ul className="mt-3 divide-y divide-line">
                {(detail.failure_reasons ?? []).map((reason) => {
                  const explained = explainError(reason.error_code, reason.error_message);
                  return (
                    <li
                      key={`${reason.error_code}-${reason.error_message}`}
                      className="flex items-start justify-between gap-4 py-2.5"
                    >
                      <div className="min-w-0">
                        <p className="text-sm font-medium">{explained?.label ?? 'Unknown error'}</p>
                        <p className="mt-0.5 text-xs text-ink-muted">
                          {explained?.detail}
                          {reason.error_code ? ` Meta code ${reason.error_code}.` : ''}
                        </p>
                      </div>
                      <span className="shrink-0 text-sm font-semibold tabular-nums">
                        {reason.count}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </Card>
          ) : null}

          {detail.template.body_preview ? (
            <Card className="mt-4">
              <h2 className="text-[11px] font-semibold uppercase tracking-section text-ink-muted">
                Message
              </h2>
              <p className="mt-2 whitespace-pre-wrap text-sm leading-relaxed">
                {detail.template.body_preview}
              </p>
            </Card>
          ) : null}

          {campaign.status === 'READY' && product.is_active ? (
            <Card className="mt-4 border-ink/10">
              {!confirming ? (
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium">Send broadcast</p>
                    <p className="mt-0.5 text-xs text-ink-muted">
                      READY means prepared, not sending. This is the explicit click.
                    </p>
                  </div>
                  <Button
                    type="button"
                    onClick={() => setConfirming(true)}
                    disabled={detail.pending_recipients === 0}
                  >
                    Send broadcast
                  </Button>
                </div>
              ) : (
                <div>
                  <p className="text-sm">
                    This will message <strong>{detail.pending_recipients}</strong> people for{' '}
                    <strong>{product.display_name}</strong>, from {product.display_name}&apos;s
                    WhatsApp number. It cannot be undone.
                  </p>
                  <div className="mt-3 flex gap-2">
                    <Button type="button" variant="danger" onClick={handleSend} disabled={busy}>
                      {busy ? 'Starting…' : `Yes, send for ${product.display_name}`}
                    </Button>
                    <Button type="button" variant="secondary" onClick={() => setConfirming(false)}>
                      Cancel
                    </Button>
                  </div>
                </div>
              )}
            </Card>
          ) : null}

          {campaign.status === 'PROCESSING' ||
          campaign.status === 'READY' ||
          campaign.status === 'PAUSED' ? (
            <div className="mt-4">
              <Button type="button" variant="danger-outline" onClick={handleCancel} disabled={busy}>
                Cancel campaign
              </Button>
              <p className="mt-1.5 text-xs text-ink-muted">
                Stops anything not yet sent. Messages already delivered cannot be recalled.
              </p>
            </div>
          ) : null}

          <div className="mb-4 mt-8 flex flex-wrap gap-2">
            {FILTERS.map((option) => (
              <FilterChip
                key={option.value}
                active={filter === option.value}
                onClick={() => {
                  setFilter(option.value);
                  setOffset(0);
                }}
              >
                {option.label}
                {option.value !== 'ALL' && detail.status_counts?.[option.value]
                  ? ` (${detail.status_counts?.[option.value]})`
                  : ''}
              </FilterChip>
            ))}
          </div>

          <div className="ui-table-wrap">
            <table className="ui-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Phone</th>
                  <th>Status</th>
                  <th>Last update</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {recipients.map((recipient) => {
                  const event = lastEvent(recipient);
                  const reason = explainError(recipient.error_code, recipient.error_message);
                  return (
                    <tr key={recipient.id}>
                      <td>{recipient.full_name ?? <span className="text-ink-muted">—</span>}</td>
                      <td className="whitespace-nowrap font-mono text-xs">
                        {formatPhone(recipient.phone_e164)}
                      </td>
                      <td>
                        <StatusBadge status={recipient.status} />
                      </td>
                      <td className="whitespace-nowrap text-xs text-ink-muted">
                        {event ? `${event.label} ${shortWhen(event.at)}` : '—'}
                      </td>
                      <td className="text-red-800" title={reason?.detail}>
                        {reason?.label ?? ''}
                        {recipient.attempts > 1 ? (
                          <span className="ml-1 text-xs text-ink-muted">
                            ({recipient.attempts} tries)
                          </span>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <Pager
            state={{ total, limit: DEFAULT_PAGE_SIZE, offset }}
            shown={recipients.length}
            onChange={setOffset}
          />
        </>
      )}
    </>
  );
}
