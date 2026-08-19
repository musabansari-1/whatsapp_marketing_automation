/**
 * Excel upload and preview (specification §6).
 *
 * The two-step shape is the specification's: upload validates and previews, and the
 * audience only becomes usable once an admin confirms what they see. Nothing is sent
 * at either step.
 */

'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

import { api, type AudienceContact, type AudienceSchema, type AudienceSummary } from '@/lib/api';

export default function UploadAudiencePage() {
  const router = useRouter();
  const [schema, setSchema] = useState<AudienceSchema | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [audience, setAudience] = useState<AudienceSummary | null>(null);
  const [sample, setSample] = useState<AudienceContact[]>([]);

  useEffect(() => {
    api
      .audienceSchema()
      .then(setSchema)
      .catch(() => setSchema(null));
  }, []);

  async function handleUpload(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file) return;

    setBusy(true);
    setError(null);
    try {
      const result = await api.uploadAudience(file, name);
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
      router.push(`/audiences/${audience.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Confirmation failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto max-w-3xl px-6 py-12">
      <Link href="/audiences" className="text-sm text-slate-600 underline">
        Back to audiences
      </Link>
      <h1 className="mt-4 text-xl font-semibold tracking-tight">Upload an audience</h1>

      {schema && (
        <section className="mt-6 rounded-lg border border-slate-200 bg-white p-5">
          <h2 className="text-sm font-medium">Expected columns</h2>
          <ul className="mt-2 space-y-1 text-sm text-slate-700">
            {schema.columns.map((column) => (
              <li key={column.source}>
                <span className="font-mono text-xs">{column.source}</span>
                {column.required ? (
                  <span className="ml-2 text-xs text-red-700">required</span>
                ) : (
                  <span className="ml-2 text-xs text-slate-400">optional</span>
                )}
              </li>
            ))}
          </ul>
          <p className="mt-3 text-xs text-slate-500">
            Numbers without a country code are read as {schema.phone_default_region} numbers. Up to{' '}
            {schema.max_rows.toLocaleString()} rows.
          </p>
          {schema.is_placeholder && (
            <p className="mt-3 rounded-md bg-amber-50 p-3 text-xs text-amber-900">
              These are development placeholder columns, not a confirmed client format. Set{' '}
              <span className="font-mono">AUDIENCE_COLUMN_SCHEMA</span> once the real column names
              are known. No code change is needed.
            </p>
          )}
        </section>
      )}

      {!audience && (
        <form onSubmit={handleUpload} className="mt-6 space-y-4">
          <div>
            <label htmlFor="name" className="block text-sm font-medium text-slate-700">
              Audience name (optional)
            </label>
            <input
              id="name"
              type="text"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Defaults to the file name"
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            />
          </div>

          <div>
            <label htmlFor="file" className="block text-sm font-medium text-slate-700">
              Excel file (.xlsx)
            </label>
            <input
              id="file"
              type="file"
              accept=".xlsx"
              required
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              className="mt-1 w-full text-sm"
            />
          </div>

          <button
            type="submit"
            disabled={busy || !file}
            className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-60"
          >
            {busy ? 'Validating...' : 'Validate file'}
          </button>
        </form>
      )}

      {error && (
        <p role="alert" className="mt-4 text-sm text-red-700">
          {error}
        </p>
      )}

      {audience && (
        <section className="mt-8">
          <h2 className="text-sm font-medium">Preview</h2>
          <div className="mt-2 grid grid-cols-4 gap-3 text-sm">
            <Stat label="Rows" value={audience.total_rows} />
            <Stat label="Valid" value={audience.valid_rows} tone="text-emerald-700" />
            <Stat label="Invalid" value={audience.invalid_rows} tone="text-red-700" />
            <Stat label="Duplicate" value={audience.duplicate_rows} tone="text-amber-700" />
          </div>

          <div className="mt-4 overflow-x-auto rounded-lg border border-slate-200 bg-white">
            <table className="w-full text-sm">
              <thead className="border-b border-slate-200 text-left text-xs uppercase text-slate-500">
                <tr>
                  <th className="px-3 py-2 font-medium">Row</th>
                  <th className="px-3 py-2 font-medium">Phone</th>
                  <th className="px-3 py-2 font-medium">Name</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {sample.map((contact) => (
                  <tr key={contact.id} className="border-b border-slate-100 last:border-0">
                    <td className="px-3 py-2 tabular-nums">{contact.row_number}</td>
                    <td className="px-3 py-2 font-mono text-xs">{contact.phone_e164 ?? '-'}</td>
                    <td className="px-3 py-2">{contact.full_name ?? '-'}</td>
                    <td className="px-3 py-2">
                      {contact.validation_status === 'VALID' ? (
                        <span className="text-emerald-700">Valid</span>
                      ) : (
                        <span className="text-red-700">
                          {contact.validation_status}
                          {contact.validation_error ? ` - ${contact.validation_error}` : ''}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-4 flex items-center gap-3">
            <button
              type="button"
              onClick={handleConfirm}
              disabled={busy || audience.valid_rows === 0}
              className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-60"
            >
              {busy ? 'Confirming...' : `Confirm ${audience.valid_rows} contacts`}
            </button>
            <Link href={`/audiences/${audience.id}`} className="text-sm underline">
              Review all rows
            </Link>
          </div>
          <p className="mt-2 text-xs text-slate-500">
            Confirming stores the audience. It does not start a campaign or send anything.
          </p>
        </section>
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
