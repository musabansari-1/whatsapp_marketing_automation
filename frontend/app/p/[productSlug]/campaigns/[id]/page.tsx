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

import { useProduct } from '@/components/product-context';
import { SegmentBadge } from '@/components/segment-filter';
import { StatusBadge } from '@/components/status-badge';
import { Alert, Button, Card, FilterChip, PageHeader, Skeleton, StatCard } from '@/components/ui';
import type { CampaignDetail, CampaignRecipient, RecipientStatus } from '@/lib/api';
import { productHref } from '@/lib/product-routes';

const FILTERS: { label: string; value: RecipientStatus | 'ALL' }[] = [
  { label: 'All', value: 'ALL' },
  { label: 'Pending', value: 'PENDING' },
  { label: 'Queued', value: 'QUEUED' },
  { label: 'Skipped', value: 'SKIPPED' },
  { label: 'Failed', value: 'FAILED' },
];

export default function CampaignDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { api, slug, product } = useProduct();

  const [detail, setDetail] = useState<CampaignDetail | null>(null);
  const [recipients, setRecipients] = useState<CampaignRecipient[]>([]);
  const [total, setTotal] = useState(0);
  const [filter, setFilter] = useState<RecipientStatus | 'ALL'>('ALL');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const load = useCallback(async () => {
    try {
      const [campaign, page] = await Promise.all([
        api.getCampaign(id),
        api.listCampaignRecipients(id, filter === 'ALL' ? undefined : filter),
      ]);
      setDetail(campaign);
      setRecipients(page.items);
      setTotal(page.total);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load this campaign.');
    }
  }, [api, id, filter]);

  useEffect(() => {
    void load();
  }, [load]);

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
              </span>
            }
          />

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard label="Recipients" value={campaign.total_recipients} />
            <StatCard label="Pending" value={detail.pending_recipients} />
            <StatCard label="Sent" value={campaign.sent_count} tone="text-forest" />
            <StatCard label="Skipped" value={campaign.skipped_count} tone="text-amber-800" />
          </div>

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
                onClick={() => setFilter(option.value)}
              >
                {option.label}
              </FilterChip>
            ))}
          </div>

          <p className="mb-2 text-xs text-ink-muted">
            Showing {recipients.length} of {total}
          </p>

          <div className="ui-table-wrap">
            <table className="ui-table">
              <thead>
                <tr>
                  <th>Phone</th>
                  <th>Status</th>
                  <th>Attempts</th>
                  <th>Error</th>
                </tr>
              </thead>
              <tbody>
                {recipients.map((recipient) => (
                  <tr key={recipient.id}>
                    <td className="font-mono text-xs">{recipient.phone_e164}</td>
                    <td>
                      <StatusBadge status={recipient.status} />
                    </td>
                    <td className="tabular-nums">{recipient.attempts}</td>
                    <td className="text-red-800">{recipient.error_message ?? ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}
