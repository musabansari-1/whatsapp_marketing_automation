/**
 * Campaign detail and the Send Broadcast confirmation (specification §7).
 *
 * Sending is the one destructive action in the platform, so it is behind an explicit
 * confirmation that states how many people will be messaged.
 */

'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';

import { api, type CampaignDetail, type CampaignRecipient, type RecipientStatus } from '@/lib/api';

const FILTERS: { label: string; value: RecipientStatus | 'ALL' }[] = [
  { label: 'All', value: 'ALL' },
  { label: 'Pending', value: 'PENDING' },
  { label: 'Queued', value: 'QUEUED' },
  { label: 'Skipped', value: 'SKIPPED' },
  { label: 'Failed', value: 'FAILED' },
];

export default function CampaignDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;

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
  }, [id, filter]);

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
    <main className="mx-auto max-w-4xl px-6 py-12">
      <Link href="/campaigns" className="text-sm text-slate-600 underline">
        Back to campaigns
      </Link>

      {error && (
        <p role="alert" className="mt-4 text-sm text-red-700">
          {error}
        </p>
      )}
      {notice && <p className="mt-4 text-sm text-emerald-700">{notice}</p>}

      {detail && campaign && (
        <>
          <div className="mt-4">
            <h1 className="text-xl font-semibold tracking-tight">{campaign.name}</h1>
            <p className="mt-1 text-sm text-slate-600">
              Status <span className="font-mono text-xs">{campaign.status}</span> &middot; template{' '}
              <span className="font-mono text-xs">{detail.template.name}</span>
            </p>
          </div>

          <div className="mt-6 grid grid-cols-4 gap-3">
            <Stat label="Recipients" value={campaign.total_recipients} />
            <Stat label="Pending" value={detail.pending_recipients} />
            <Stat label="Sent" value={campaign.sent_count} tone="text-emerald-700" />
            <Stat label="Skipped" value={campaign.skipped_count} tone="text-amber-700" />
          </div>

          {detail.template.body_preview && (
            <div className="mt-4 rounded-lg border border-slate-200 bg-white p-4">
              <h2 className="text-xs font-medium uppercase text-slate-500">Message</h2>
              <p className="mt-2 whitespace-pre-wrap text-sm">{detail.template.body_preview}</p>
            </div>
          )}

          {campaign.status === 'READY' && (
            <div className="mt-6 rounded-lg border border-slate-300 bg-white p-5">
              {!confirming ? (
                <button
                  type="button"
                  onClick={() => setConfirming(true)}
                  disabled={detail.pending_recipients === 0}
                  className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-60"
                >
                  Send broadcast
                </button>
              ) : (
                <div>
                  <p className="text-sm">
                    This will message <strong>{detail.pending_recipients}</strong> people. It cannot
                    be undone.
                  </p>
                  <div className="mt-3 flex gap-2">
                    <button
                      type="button"
                      onClick={handleSend}
                      disabled={busy}
                      className="rounded-md bg-red-700 px-3 py-2 text-sm font-medium text-white disabled:opacity-60"
                    >
                      {busy ? 'Starting...' : 'Yes, send now'}
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirming(false)}
                      className="rounded-md border border-slate-300 px-3 py-2 text-sm"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}

          {(campaign.status === 'PROCESSING' ||
            campaign.status === 'READY' ||
            campaign.status === 'PAUSED') && (
            <div className="mt-4">
              <button
                type="button"
                onClick={handleCancel}
                disabled={busy}
                className="rounded-md border border-red-300 px-3 py-2 text-sm text-red-700 disabled:opacity-60"
              >
                Cancel campaign
              </button>
              <p className="mt-1 text-xs text-slate-500">
                Stops anything not yet sent. Messages already delivered cannot be recalled.
              </p>
            </div>
          )}

          <div className="mt-6 flex gap-2">
            {FILTERS.map((option) => (
              <button
                key={option.value}
                type="button"
                onClick={() => setFilter(option.value)}
                className={`rounded-md border px-3 py-1.5 text-sm ${
                  filter === option.value
                    ? 'border-slate-900 bg-slate-900 text-white'
                    : 'border-slate-300 text-slate-700'
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>

          <p className="mt-3 text-xs text-slate-500">
            Showing {recipients.length} of {total}
          </p>

          <div className="mt-2 overflow-x-auto rounded-lg border border-slate-200 bg-white">
            <table className="w-full text-sm">
              <thead className="border-b border-slate-200 text-left text-xs uppercase text-slate-500">
                <tr>
                  <th className="px-3 py-2 font-medium">Phone</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Attempts</th>
                  <th className="px-3 py-2 font-medium">Error</th>
                </tr>
              </thead>
              <tbody>
                {recipients.map((recipient) => (
                  <tr key={recipient.id} className="border-b border-slate-100 last:border-0">
                    <td className="px-3 py-2 font-mono text-xs">{recipient.phone_e164}</td>
                    <td className="px-3 py-2">{recipient.status}</td>
                    <td className="px-3 py-2 tabular-nums">{recipient.attempts}</td>
                    <td className="px-3 py-2 text-red-700">{recipient.error_message ?? ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </main>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3">
      <div className="text-xs text-slate-500">{label}</div>
      <div className={`mt-1 text-lg font-semibold tabular-nums ${tone ?? ''}`}>{value}</div>
    </div>
  );
}
