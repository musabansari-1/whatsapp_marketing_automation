/**
 * Campaigns list (specification §17).
 *
 * Minimal by design; the full dashboard is Phase 13.
 */

'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';

import { AppShell } from '@/components/shell';
import { StatusBadge } from '@/components/status-badge';
import { Alert, ButtonLink, EmptyState, PageHeader, Skeleton } from '@/components/ui';
import { api, type Campaign } from '@/lib/api';

export default function CampaignsPage() {
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .listCampaigns()
      .then(setCampaigns)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  return (
    <AppShell>
      <PageHeader
        title="Campaigns"
        description="Creating a campaign prepares it. Sending is a separate, explicit action."
        actions={<ButtonLink href="/campaigns/new">New campaign</ButtonLink>}
      />

      {error ? <Alert>{error}</Alert> : null}

      {loading ? (
        <Skeleton className="h-48" />
      ) : campaigns.length === 0 ? (
        <EmptyState
          title="No campaigns yet"
          description="A campaign freezes its audience and template. READY means prepared, not sending."
          action={<ButtonLink href="/campaigns/new">New campaign</ButtonLink>}
        />
      ) : (
        <div className="ui-table-wrap">
          <table className="ui-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Status</th>
                <th className="text-right">Recipients</th>
                <th className="text-right">Sent</th>
                <th className="text-right">Skipped</th>
              </tr>
            </thead>
            <tbody>
              {campaigns.map((campaign) => (
                <tr key={campaign.id}>
                  <td>
                    <Link
                      href={`/campaigns/${campaign.id}`}
                      className="font-medium text-ink hover:text-forest"
                    >
                      {campaign.name}
                    </Link>
                  </td>
                  <td>
                    <StatusBadge status={campaign.status} />
                  </td>
                  <td className="text-right tabular-nums">{campaign.total_recipients}</td>
                  <td className="text-right tabular-nums">{campaign.sent_count}</td>
                  <td className="text-right tabular-nums">{campaign.skipped_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </AppShell>
  );
}
