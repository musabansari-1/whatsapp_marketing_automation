/**
 * Excel upload and preview (specification §6, §28).
 *
 * The two-step shape is the specification's: upload validates and previews, and the
 * audience only becomes usable once an admin confirms what they see. Nothing is sent at
 * either step.
 *
 * The audience belongs to the product in the address. A segment is optional — a
 * product-wide audience is normal, and a product may have no segments at all.
 */

'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

import { useProduct } from '@/components/product-context';
import { StatusBadge } from '@/components/status-badge';
import { Alert, Button, Card, PageHeader, StatCard } from '@/components/ui';
import type { AudienceContact, AudienceSchema, AudienceSummary } from '@/lib/api';
import { productHref } from '@/lib/product-routes';

export default function UploadAudiencePage() {
  const router = useRouter();
  const { api, slug, product } = useProduct();
  const [schema, setSchema] = useState<AudienceSchema | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState('');
  const [segmentId, setSegmentId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [audience, setAudience] = useState<AudienceSummary | null>(null);
  const [sample, setSample] = useState<AudienceContact[]>([]);

  const segments = product.segments.filter((segment) => segment.is_active);

  useEffect(() => {
    api
      .audienceSchema()
      .then(setSchema)
      .catch(() => setSchema(null));
  }, [api]);

  async function handleUpload(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file) return;

    setBusy(true);
    setError(null);
    try {
      const result = await api.uploadAudience(file, name, segmentId || undefined);
      setAudience(result.audience);
      setSample(result.sample);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Upload failed.');
    } finally {
      setBusy(false);
    }
  }

  async function handleConfirm() {
    if (!audience) return;
    setBusy(true);
    setError(null);
    try {
      await api.confirmAudience(audience.id);
      router.push(productHref(slug, `/audiences/${audience.id}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Confirmation failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        back={{ href: productHref(slug, '/audiences'), label: 'Audiences' }}
        title={`Upload an audience for ${product.display_name}`}
        description="Validate first. Confirming stores the contacts as this product's leads; it still does not send anything."
      />

      {!product.is_active ? (
        <Alert tone="warning" className="mb-6">
          {product.display_name} is inactive, so new audiences are refused.
        </Alert>
      ) : null}

      {schema ? (
        <Card className="mb-6">
          <h2 className="text-sm font-semibold">Expected columns</h2>
          <ul className="mt-3 grid gap-2 sm:grid-cols-2">
            {schema.columns.map((column) => (
              <li
                key={column.source}
                className="flex items-center justify-between rounded-lg bg-canvas px-3 py-2 text-sm"
              >
                <span className="font-mono text-xs">{column.source}</span>
                {column.required ? (
                  <span className="text-[11px] font-semibold uppercase tracking-wide text-red-700">
                    required
                  </span>
                ) : (
                  <span className="text-[11px] uppercase tracking-wide text-ink-faint">
                    optional
                  </span>
                )}
              </li>
            ))}
          </ul>
          <p className="mt-3 text-xs text-ink-muted">
            Numbers without a country code are read as {schema.phone_default_region} numbers. Up to{' '}
            {schema.max_rows.toLocaleString()} rows.
          </p>
          {schema.is_placeholder ? (
            <Alert tone="warning" className="mt-3">
              These are development placeholder columns, not a confirmed client format. Set{' '}
              <span className="font-mono">AUDIENCE_COLUMN_SCHEMA</span> once the real column names
              are known. No code change is needed.
            </Alert>
          ) : null}
        </Card>
      ) : null}

      {!audience ? (
        <form onSubmit={handleUpload} className="space-y-4">
          <div>
            <label htmlFor="name" className="block text-sm font-medium text-ink">
              Audience name (optional)
            </label>
            <input
              id="name"
              type="text"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Defaults to the file name"
              className="ui-input mt-1.5"
            />
          </div>

          {segments.length > 0 ? (
            <div>
              <label htmlFor="segment" className="block text-sm font-medium text-ink">
                Segment (optional)
              </label>
              <select
                id="segment"
                value={segmentId}
                onChange={(event) => setSegmentId(event.target.value)}
                className="ui-select mt-1.5"
              >
                <option value="">Whole product</option>
                {segments.map((segment) => (
                  <option key={segment.id} value={segment.id}>
                    {segment.display_name}
                  </option>
                ))}
              </select>
            </div>
          ) : null}

          <div>
            <label htmlFor="file" className="block text-sm font-medium text-ink">
              Excel file (.xlsx)
            </label>
            <label
              htmlFor="file"
              className="mt-1.5 flex cursor-pointer flex-col items-center justify-center rounded-xl border border-dashed border-line bg-white px-6 py-10 text-center shadow-card transition hover:border-forest/40"
            >
              <span className="text-sm font-medium text-ink">
                {file ? file.name : 'Choose a spreadsheet'}
              </span>
              <span className="mt-1 text-xs text-ink-muted">
                .xlsx only. Nothing is sent at this step.
              </span>
            </label>
            <input
              id="file"
              type="file"
              accept=".xlsx"
              required
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              className="sr-only"
            />
          </div>

          <Button type="submit" disabled={busy || !file || !product.is_active}>
            {busy ? 'Validating…' : 'Validate file'}
          </Button>
        </form>
      ) : null}

      {error ? <Alert className="mt-4">{error}</Alert> : null}

      {audience ? (
        <section className="mt-2 space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard label="Rows" value={audience.total_rows} />
            <StatCard label="Valid" value={audience.valid_rows} tone="text-forest" />
            <StatCard label="Invalid" value={audience.invalid_rows} tone="text-red-800" />
            <StatCard label="Duplicate" value={audience.duplicate_rows} tone="text-amber-800" />
          </div>

          <div className="ui-table-wrap">
            <table className="ui-table">
              <thead>
                <tr>
                  <th>Row</th>
                  <th>Phone</th>
                  <th>Name</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {sample.map((contact) => (
                  <tr key={contact.id}>
                    <td className="tabular-nums">{contact.row_number}</td>
                    <td className="font-mono text-xs">{contact.phone_e164 ?? '—'}</td>
                    <td>{contact.full_name ?? '—'}</td>
                    <td>
                      {contact.validation_status === 'VALID' ? (
                        <StatusBadge status="VALID" />
                      ) : (
                        <span className="text-sm text-red-800">
                          {contact.validation_status}
                          {contact.validation_error ? ` — ${contact.validation_error}` : ''}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <Button
              type="button"
              onClick={handleConfirm}
              disabled={busy || audience.valid_rows === 0}
            >
              {busy ? 'Confirming…' : `Confirm ${audience.valid_rows} contacts`}
            </Button>
            <Link
              href={productHref(slug, `/audiences/${audience.id}`)}
              className="text-sm font-medium text-ink-muted hover:text-ink"
            >
              Review all rows
            </Link>
          </div>
          <p className="text-xs text-ink-muted">
            Confirming stores the audience. It does not start a campaign or send anything.
          </p>
        </section>
      ) : null}
    </>
  );
}
