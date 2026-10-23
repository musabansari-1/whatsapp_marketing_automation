/**
 * Product overview (specification §17, [D53]).
 *
 * The numbers that answer "is this working?" for one product, optionally narrowed to a
 * segment. The same component renders the organisation's combined figures on the home
 * page; only the scope differs.
 */

'use client';

import { useCallback, useState } from 'react';

import { OverviewMetrics } from '@/components/overview-metrics';
import { useProduct } from '@/components/product-context';
import { SegmentFilter } from '@/components/segment-filter';
import { Alert, PageHeader } from '@/components/ui';
import { productHref } from '@/lib/product-routes';

export default function ProductOverviewPage() {
  const { product, api, slug } = useProduct();
  const [segment, setSegment] = useState('');

  const load = useCallback(
    () =>
      Promise.all([api.getOverview(segment || undefined), api.getLeadFunnel(segment || undefined)]),
    [api, segment],
  );

  return (
    <>
      <PageHeader
        title="Overview"
        description={`Reach, engagement and outcomes for ${product.display_name}.`}
        actions={<SegmentFilter value={segment} onChange={setSegment} />}
      />

      {segment ? (
        <Alert tone="info" className="mb-6">
          Leads count through audience membership, so a lead in several segments counts in each, and
          segment figures do not add up to the product&apos;s.
        </Alert>
      ) : null}

      <OverviewMetrics
        load={load}
        links={{
          campaigns: productHref(slug, '/campaigns'),
          conversations: productHref(slug, '/conversations'),
          leads: productHref(slug, '/leads'),
          bookings: productHref(slug, '/bookings'),
        }}
        showDemos={product.has_calendar}
      />
    </>
  );
}
