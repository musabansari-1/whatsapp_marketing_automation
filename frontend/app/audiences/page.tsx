/**
 * Audiences list (specification §17).
 *
 * Minimal by design: the full dashboard is Phase 13. This exists so Phase 4 is
 * usable end to end rather than API-only.
 */

'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';

import { AppShell } from '@/components/shell';
import { StatusBadge } from '@/components/status-badge';
import { Alert, ButtonLink, EmptyState, PageHeader, Skeleton } from '@/components/ui';
import { api, type AudienceSummary } from '@/lib/api';

export default function AudiencesPage() {
  const [audiences, setAudiences] = useState<AudienceSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .listAudiences()
      .then(setAudiences)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  return (
    <AppShell>
      <PageHeader
        title="Audiences"
        description="Uploading validates a spreadsheet. It never sends messages."
        actions={<ButtonLink href="/audiences/upload">Upload Excel</ButtonLink>}
      />

      {error ? <Alert>{error}</Alert> : null}

      {loading ? (
        <Skeleton className="h-48" />
      ) : audiences.length === 0 ? (
        <EmptyState
          title="No audiences yet"
          description="Upload an Excel file to validate contacts. Confirmation is a separate step, and still sends nothing."
          action={<ButtonLink href="/audiences/upload">Upload Excel</ButtonLink>}
        />
      ) : (
        <div className="ui-table-wrap">
          <table className="ui-table">
            <thead>
              <tr>
                <th>Name</th>
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
                      href={`/audiences/${audience.id}`}
                      className="font-medium text-ink hover:text-forest"
                    >
                      {audience.name}
                    </Link>
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
    </AppShell>
  );
}
