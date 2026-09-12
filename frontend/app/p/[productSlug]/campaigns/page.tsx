/**
 * Campaigns, for the active product (specification §7, §17, §28).
 *
 * Creating a campaign prepares it; sending is a separate, explicit action. Every campaign
 * here broadcasts for the product in the address, from that product's WhatsApp number.
 */

'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';

import { useProduct } from '@/components/product-context';
import { SegmentBadge, SegmentFilter } from '@/components/segment-filter';
import { StatusBadge } from '@/components/status-badge';
import { Alert, ButtonLink, EmptyState, PageHeader, Skeleton } from '@/components/ui';
import type { Campaign } from '@/lib/api';
import { productHref } from '@/lib/product-routes';

export default function CampaignsPage() {
  const { api, slug, product } = useProduct();
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [segment, setSegment] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api
      .listCampaigns(segment || undefined)
      .then(setCampaigns)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [api, segment]);

  const create = product.is_active ? (
    <ButtonLink href={productHref(slug, '/campaigns/new')}>New campaign</ButtonLink>
  ) : null;

  return (
    <>
      <PageHeader
        title="Campaigns"
        description={`${product.display_name}'s broadcasts. Creating one prepares it; sending is a separate, explicit action.`}
        actions={create}
      />

      <div className="mb-6">
        <SegmentFilter value={segment} onChange={setSegment} />
      </div>

      {error ? <Alert>{error}</Alert> : null}

      {loading ? (
        <Skeleton className="h-48" />
      ) : campaigns.length === 0 ? (
        <EmptyState
          title={segment ? 'No campaigns in this segment' : 'No campaigns yet'}
          description="A campaign freezes its audience and template. READY means prepared, not sending."
          action={create ?? undefined}
        />
      ) : (
        <div className="ui-table-wrap">
          <table className="ui-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Segment</th>
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
                      href={productHref(slug, `/campaigns/${campaign.id}`)}
                      className="font-medium text-ink hover:text-forest"
                    >
                      {campaign.name}
                    </Link>
                  </td>
                  <td>
                    {campaign.segment_id ? (
                      <SegmentBadge segmentId={campaign.segment_id} />
                    ) : (
                      <span className="text-xs text-ink-faint">Whole product</span>
                    )}
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
    </>
  );
}
