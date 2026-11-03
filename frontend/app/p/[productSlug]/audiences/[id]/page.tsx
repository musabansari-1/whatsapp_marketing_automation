/**
 * Audience detail (specification §6, §17).
 *
 * Shows every parsed row with its validation outcome, filterable by status, because §6
 * requires the admin to be able to see what was rejected and why. Confirming promotes the
 * valid rows to this product's leads.
 */

'use client';

import { useParams } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';

import { Pager } from '@/app/pager';
import { useProduct } from '@/components/product-context';
import { SegmentBadge } from '@/components/segment-filter';
import { StatusBadge } from '@/components/status-badge';
import { Alert, Button, FilterChip, PageHeader, Skeleton } from '@/components/ui';
import type { AudienceContact, AudienceSummary, ValidationStatus } from '@/lib/api';
import { DEFAULT_PAGE_SIZE } from '@/lib/pagination';
import { productHref } from '@/lib/product-routes';

const FILTERS: { label: string; value: ValidationStatus | 'ALL' }[] = [
  { label: 'All', value: 'ALL' },
  { label: 'Valid', value: 'VALID' },
  { label: 'Invalid', value: 'INVALID' },
  { label: 'Duplicate', value: 'DUPLICATE' },
];

export default function AudienceDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { api, slug, product } = useProduct();

  const [audience, setAudience] = useState<AudienceSummary | null>(null);
  const [contacts, setContacts] = useState<AudienceContact[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState<ValidationStatus | 'ALL'>('ALL');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // The rename form's draft; null while the form is closed.
  const [draftName, setDraftName] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [summary, page] = await Promise.all([
        api.getAudience(id),
        api.listContacts(id, filter === 'ALL' ? undefined : filter, DEFAULT_PAGE_SIZE, offset),
      ]);
      setAudience(summary);
      setContacts(page.items);
      setTotal(page.total);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load this audience.');
    }
  }, [api, id, filter, offset]);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleConfirm() {
    setBusy(true);
    setError(null);
    try {
      await api.confirmAudience(id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Confirmation failed.');
    } finally {
      setBusy(false);
    }
  }

  async function handleRename() {
    const name = (draftName ?? '').trim();
    if (!name) {
      setError('The name cannot be blank.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.renameAudience(id, name);
      setDraftName(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not rename the audience.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {error ? <Alert className="mb-4">{error}</Alert> : null}

      {!audience ? (
        error ? null : (
          <Skeleton className="h-64" />
        )
      ) : (
        <>
          <PageHeader
            back={{ href: productHref(slug, '/audiences'), label: 'Audiences' }}
            title={audience.name}
            description={
              <span className="flex flex-wrap items-center gap-2">
                <SegmentBadge segmentId={audience.segment_id} />
                <span>
                  {audience.total_rows} rows · {audience.valid_rows} valid · {audience.invalid_rows}{' '}
                  invalid · {audience.duplicate_rows} duplicate
                </span>
              </span>
            }
            actions={
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => setDraftName(audience.name)}
                  disabled={busy}
                >
                  Rename
                </Button>
                {audience.status === 'PENDING_CONFIRMATION' ? (
                  product.is_active ? (
                    <Button
                      type="button"
                      onClick={handleConfirm}
                      disabled={busy || audience.valid_rows === 0}
                    >
                      {busy ? 'Confirming…' : `Confirm into ${product.display_name}`}
                    </Button>
                  ) : null
                ) : (
                  <StatusBadge status="CONFIRMED" />
                )}
              </div>
            }
          />

          {draftName !== null ? (
            <form
              className="mb-4 flex flex-wrap items-end gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                void handleRename();
              }}
            >
              <label className="min-w-64 flex-1 text-sm font-medium">
                New name
                <input
                  className="ui-input mt-1.5"
                  value={draftName}
                  maxLength={255}
                  autoFocus
                  onChange={(event) => setDraftName(event.target.value)}
                />
              </label>
              <Button type="submit" disabled={busy || draftName.trim() === ''}>
                {busy ? 'Saving…' : 'Save name'}
              </Button>
              <Button type="button" variant="secondary" onClick={() => setDraftName(null)}>
                Cancel
              </Button>
            </form>
          ) : null}

          <div className="mb-4 flex flex-wrap gap-2">
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
              </FilterChip>
            ))}
          </div>

          <div className="ui-table-wrap">
            <table className="ui-table">
              <thead>
                <tr>
                  <th>Row</th>
                  <th>Phone</th>
                  <th>Name</th>
                  <th>Email</th>
                  <th>Company</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {contacts.map((contact) => (
                  <tr key={contact.id}>
                    <td className="tabular-nums">{contact.row_number}</td>
                    <td className="font-mono text-xs">{contact.phone_e164 ?? '—'}</td>
                    <td>{contact.full_name ?? '—'}</td>
                    <td>{contact.email ?? '—'}</td>
                    <td>{contact.company ?? '—'}</td>
                    <td>
                      {contact.validation_status === 'VALID' ? (
                        <StatusBadge status="VALID" />
                      ) : (
                        <span
                          className={
                            contact.validation_status === 'DUPLICATE'
                              ? 'text-sm text-amber-800'
                              : 'text-sm text-red-800'
                          }
                        >
                          {contact.validation_error ?? contact.validation_status}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager
            state={{ total, limit: DEFAULT_PAGE_SIZE, offset }}
            shown={contacts.length}
            onChange={setOffset}
          />
        </>
      )}
    </>
  );
}
