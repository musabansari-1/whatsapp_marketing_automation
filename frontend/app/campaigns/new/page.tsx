/**
 * Campaign creation (specification §7).
 *
 * Selects a confirmed audience and an approved template. The template list comes from
 * Meta via the Phase 6 synchronization ([D6]); until that exists the list is empty and
 * a campaign cannot be created, which this page states plainly rather than failing
 * with an unexplained empty dropdown.
 */

'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

import { api, type AudienceSummary, type WhatsAppTemplate } from '@/lib/api';

export default function NewCampaignPage() {
  const router = useRouter();
  const [audiences, setAudiences] = useState<AudienceSummary[]>([]);
  const [templates, setTemplates] = useState<WhatsAppTemplate[]>([]);
  const [name, setName] = useState('');
  const [audienceId, setAudienceId] = useState('');
  const [templateId, setTemplateId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([api.listAudiences(), api.listTemplates()])
      .then(([allAudiences, allTemplates]) => {
        // Only a confirmed audience has leads to snapshot (§6).
        setAudiences(allAudiences.filter((a) => a.status === 'CONFIRMED'));
        setTemplates(allTemplates);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const detail = await api.createCampaign(name, audienceId, templateId);
      router.push(`/campaigns/${detail.campaign.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not create the campaign.');
    } finally {
      setBusy(false);
    }
  }

  const selectedTemplate = templates.find((t) => t.id === templateId);

  return (
    <main className="mx-auto max-w-3xl px-6 py-12">
      <Link href="/campaigns" className="text-sm text-slate-600 underline">
        Back to campaigns
      </Link>
      <h1 className="mt-4 text-xl font-semibold tracking-tight">New campaign</h1>

      {loading ? (
        <p className="mt-6 text-sm text-slate-500">Loading...</p>
      ) : (
        <>
          {templates.length === 0 && (
            <p className="mt-6 rounded-md bg-amber-50 p-3 text-sm text-amber-900">
              No approved marketing templates are available. Templates are approved by Meta and
              synchronized into this platform; that synchronization arrives in Phase 6. A campaign
              cannot be created until at least one approved template exists.
            </p>
          )}

          {audiences.length === 0 && (
            <p className="mt-4 rounded-md bg-amber-50 p-3 text-sm text-amber-900">
              No confirmed audiences. Upload and confirm one first.
            </p>
          )}

          <form onSubmit={handleSubmit} className="mt-6 space-y-4">
            <div>
              <label htmlFor="name" className="block text-sm font-medium text-slate-700">
                Campaign name
              </label>
              <input
                id="name"
                type="text"
                required
                value={name}
                onChange={(event) => setName(event.target.value)}
                className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
              />
            </div>

            <div>
              <label htmlFor="audience" className="block text-sm font-medium text-slate-700">
                Audience
              </label>
              <select
                id="audience"
                required
                value={audienceId}
                onChange={(event) => setAudienceId(event.target.value)}
                className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
              >
                <option value="">Select a confirmed audience</option>
                {audiences.map((audience) => (
                  <option key={audience.id} value={audience.id}>
                    {audience.name} ({audience.valid_rows} contacts)
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label htmlFor="template" className="block text-sm font-medium text-slate-700">
                Approved template
              </label>
              <select
                id="template"
                required
                value={templateId}
                onChange={(event) => setTemplateId(event.target.value)}
                className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
              >
                <option value="">Select a template</option>
                {templates.map((template) => (
                  <option key={template.id} value={template.id}>
                    {template.name} ({template.language})
                  </option>
                ))}
              </select>
            </div>

            {selectedTemplate?.body_preview && (
              <div className="rounded-lg border border-slate-200 bg-white p-4">
                <h2 className="text-xs font-medium uppercase text-slate-500">Template preview</h2>
                <p className="mt-2 whitespace-pre-wrap text-sm">{selectedTemplate.body_preview}</p>
              </div>
            )}

            {error && (
              <p role="alert" className="text-sm text-red-700">
                {error}
              </p>
            )}

            <button
              type="submit"
              disabled={busy || !audienceId || !templateId}
              className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-60"
            >
              {busy ? 'Preparing...' : 'Create campaign'}
            </button>
            <p className="text-xs text-slate-500">
              Creating prepares the campaign and freezes its recipients. It does not send anything.
            </p>
          </form>
        </>
      )}
    </main>
  );
}
