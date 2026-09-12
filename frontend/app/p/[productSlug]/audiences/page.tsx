/**
 * Audiences, for the active product (specification §6, §17, §28).
 *
 * Uploading validates a spreadsheet and never sends a message. Every audience here belongs
 * to the product in the address; a segment, when an audience has one, narrows who it
 * targets within that product.
 */

'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';

import { useProduct } from '@/components/product-context';
import { SegmentBadge, SegmentFilter } from '@/components/segment-filter';
import { StatusBadge } from '@/components/status-badge';
import { Alert, ButtonLink, EmptyState, PageHeader, Skeleton } from '@/components/ui';
import type { AudienceSummary } from '@/lib/api';
import { productHref } from '@/lib/product-routes';

export default function AudiencesPage() {
  const { api, slug, product } = useProduct();
  const [audiences, setAudiences] = useState<AudienceSummary[]>([]);
  const [segment, setSegment] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api
      .listAudiences(segment || undefined)
      .then(setAudiences)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [api, segment]);

  const upload = product.is_active ? (
    <ButtonLink href={productHref(slug, '/audiences/upload')}>Upload Excel</ButtonLink>
  ) : null;

  return (
    <>
      <PageHeader
        title="Audiences"
        description={`${product.display_name}'s contact lists. Uploading validates a spreadsheet; it never sends messages.`}
        actions={upload}
      />

      <div className="mb-6">
        <SegmentFilter value={segment} onChange={setSegment} />
      </div>

      {error ? <Alert>{error}</Alert> : null}

      {loading ? (
        <Skeleton className="h-48" />
      ) : audiences.length === 0 ? (
        <EmptyState
          title={segment ? 'No audiences in this segment' : 'No audiences yet'}
          description="Upload an Excel file to validate contacts. Confirmation is a separate step, and still sends nothing."
          action={upload ?? undefined}
        />
      ) : (
        <div className="ui-table-wrap">
          <table className="ui-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Segment</th>
                <th>Status</th>
                <th className="text-right">Valid</th>
                <th className="text-right">Invalid</th>
                <th className="text-right">Duplicate</th>
              </tr>
            </thead>
            <tbody>
              {audiences.map((audience) => (
                <tr key={audience.id}>
                  <td>
                    <Link
                      href={productHref(slug, `/audiences/${audience.id}`)}
                      className="font-medium text-ink hover:text-forest"
                    >
                      {audience.name}
                    </Link>
                  </td>
                  <td>
                    {audience.segment_id ? (
                      <SegmentBadge segmentId={audience.segment_id} />
                    ) : (
                      <span className="text-xs text-ink-faint">Whole product</span>
                    )}
                  </td>
                  <td>
                    <StatusBadge
                      status={
                        audience.status === 'CONFIRMED' ? 'CONFIRMED' : 'PENDING_CONFIRMATION'
                      }
                    />
                  </td>
                  <td className="text-right tabular-nums">{audience.valid_rows}</td>
                  <td className="text-right tabular-nums">{audience.invalid_rows}</td>
                  <td className="text-right tabular-nums">{audience.duplicate_rows}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
