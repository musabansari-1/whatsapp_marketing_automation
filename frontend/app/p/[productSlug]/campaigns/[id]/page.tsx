/**
 * Campaign detail and the Send Broadcast confirmation (specification §7, §28).
 *
 * Sending is the one destructive action in the platform, so it is behind an explicit
 * confirmation that states how many people will be messaged — and for which product, from
 * whose number, since that is the mistake a multi-product dashboard makes possible.
 *
 * Scheduling is the same confirmation given for a later moment, so it states the same
 * things plus when. A scheduled campaign starts on its own; if it cannot, it comes back
 * here as READY with a warning, and the bell says why.
 */

'use client';

import Link from 'next/link';
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
import {
  defaultScheduleTime,
  fromLocalInputValue,
  HANDED_BACK_EVENTS,
  MIN_LEAD_MS,
  scheduleLabel,
  scheduleProblem,
  timeUntil,
  toLocalInputValue,
} from '@/lib/schedule';

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
// While a broadcast is scheduled, refresh so the page shows it starting.
const SCHEDULED_REFRESH_MS = 30_000;

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
  const [scheduling, setScheduling] = useState(false);
  // The picker's value, a `datetime-local` string in the viewer's clock.
  const [picked, setPicked] = useState('');
  const [pickerMin, setPickerMin] = useState('');

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
  const isScheduled = detail?.campaign.status === 'SCHEDULED';
  useEffect(() => {
    if (!sending && !isScheduled) return;
    const timer = window.setInterval(
      () => void load(),
      sending ? LIVE_REFRESH_MS : SCHEDULED_REFRESH_MS,
    );
    return () => window.clearInterval(timer);
  }, [sending, isScheduled, load]);

  function openScheduler() {
    const current = detail?.campaign.scheduled_for;
    const now = new Date();
    setPicked(toLocalInputValue(current ? new Date(current) : defaultScheduleTime(now)));
    setPickerMin(toLocalInputValue(new Date(now.getTime() + MIN_LEAD_MS)));
    setConfirming(false);
    setScheduling(true);
    setError(null);
    setNotice(null);
  }

  async function handleSchedule() {
    const when = fromLocalInputValue(picked);
    const problem = scheduleProblem(when);
    if (problem || !when) {
      setError(problem ?? 'Pick a date and time.');
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await api.scheduleCampaign(id, when);
      setNotice(
        `Scheduled for ${scheduleLabel(updated.scheduled_for ?? when.toISOString())}. ` +
          'It will start on its own at that time. Nothing has been sent yet.',
      );
      setScheduling(false);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not schedule the campaign.');
    } finally {
      setBusy(false);
    }
  }

  async function handleUnschedule() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api.unscheduleCampaign(id);
      setNotice('Schedule removed. The campaign is READY again; nothing was sent.');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not remove the schedule.');
    } finally {
      setBusy(false);
    }
  }

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

  // A scheduled start that could not go ahead returns the campaign to READY. Say so
  // here too, not only in the bell, since this is where someone will look.
  const activity = detail?.activity ?? [];
  const latestActivity = activity.at(-1)?.event_type ?? null;
  const handedBack =
    campaign?.status === 'READY' &&
    latestActivity !== null &&
    (HANDED_BACK_EVENTS as readonly string[]).includes(latestActivity);

  const pickedWhen = scheduling ? fromLocalInputValue(picked) : null;
  const pickedProblem = scheduling ? scheduleProblem(pickedWhen) : null;
  const pending = detail?.pending_recipients ?? 0;

  const sendConfirm = (
    <div>
      <p className="text-sm">
        This will message <strong>{pending}</strong> people for{' '}
        <strong>{product.display_name}</strong>, from {product.display_name}&apos;s WhatsApp number.
        It cannot be undone.
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
  );

  const scheduler = (
    <div>
      <label htmlFor="schedule-at" className="text-sm font-medium">
        Send at
      </label>
      <input
        id="schedule-at"
        type="datetime-local"
        className="ui-input mt-1.5 max-w-xs"
        value={picked}
        min={pickerMin}
        onChange={(event) => setPicked(event.target.value)}
      />
      <p className={`mt-1.5 text-xs ${pickedProblem ? 'text-red-700' : 'text-ink-muted'}`}>
        {pickedProblem ??
          (pickedWhen
            ? `${scheduleLabel(pickedWhen.toISOString())} your time · starts ${timeUntil(
                pickedWhen.toISOString(),
              )}`
            : null)}
      </p>
      <p className="mt-3 text-sm">
        At that time this will message <strong>{pending}</strong> people for{' '}
        <strong>{product.display_name}</strong>, from {product.display_name}&apos;s WhatsApp number.
        Opt-outs and the template are checked again then; if it cannot start, it comes back here as
        READY and you are notified.
      </p>
      <div className="mt-3 flex gap-2">
        <Button
          type="button"
          variant="danger"
          onClick={handleSchedule}
          disabled={busy || pickedProblem !== null}
        >
          {busy ? 'Scheduling…' : `Yes, schedule for ${product.display_name}`}
        </Button>
        <Button type="button" variant="secondary" onClick={() => setScheduling(false)}>
          Back
        </Button>
      </div>
    </div>
  );

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
                {campaign.status === 'SCHEDULED' && campaign.scheduled_for ? (
                  <span>· sends {scheduleLabel(campaign.scheduled_for)}</span>
                ) : null}
                {campaign.started_at ? (
                  <span>· started {shortWhen(campaign.started_at)}</span>
                ) : null}
                {campaign.completed_at ? (
                  <span>· finished {shortWhen(campaign.completed_at)}</span>
                ) : null}
              </span>
            }
          />

          {handedBack ? (
            <Alert tone="warning" className="mb-4">
              {latestActivity === 'SCHEDULE_MISSED'
                ? 'The scheduled send was missed and was not sent late. '
                : 'The scheduled send could not start. '}
              Nothing was sent; the campaign is READY again. The reason is in{' '}
              <Link href="/notifications" className="font-semibold underline">
                Notifications
              </Link>
              . Fix it, then schedule it again or send it now.
            </Alert>
          ) : null}

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
              {scheduling ? (
                scheduler
              ) : confirming ? (
                sendConfirm
              ) : (
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium">Send broadcast</p>
                    <p className="mt-0.5 text-xs text-ink-muted">
                      READY means prepared, not sending. Send it now, or schedule a time and it
                      starts on its own.
                    </p>
                  </div>
                  <div className="flex gap-2">
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={openScheduler}
                      disabled={pending === 0}
                    >
                      Schedule
                    </Button>
                    <Button
                      type="button"
                      onClick={() => setConfirming(true)}
                      disabled={pending === 0}
                    >
                      Send broadcast
                    </Button>
                  </div>
                </div>
              )}
            </Card>
          ) : null}

          {campaign.status === 'SCHEDULED' && campaign.scheduled_for ? (
            <Card className="mt-4 border-ink/10">
              {scheduling ? (
                scheduler
              ) : confirming ? (
                sendConfirm
              ) : (
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium">
                      Scheduled for {scheduleLabel(campaign.scheduled_for)} (
                      {timeUntil(campaign.scheduled_for)})
                    </p>
                    <p className="mt-0.5 text-xs text-ink-muted">
                      It starts on its own at that time; nothing has been sent yet. If it cannot
                      start, it comes back here as READY and you are notified.
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button type="button" variant="secondary" onClick={openScheduler}>
                      Change time
                    </Button>
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={handleUnschedule}
                      disabled={busy}
                    >
                      Unschedule
                    </Button>
                    {product.is_active ? (
                      <Button type="button" onClick={() => setConfirming(true)}>
                        Send now
                      </Button>
                    ) : null}
                  </div>
                </div>
              )}
            </Card>
          ) : null}

          {campaign.status === 'PROCESSING' ||
          campaign.status === 'READY' ||
          campaign.status === 'SCHEDULED' ||
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
