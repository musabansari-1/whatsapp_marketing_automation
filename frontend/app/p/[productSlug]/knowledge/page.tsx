/**
 * Knowledge base, for the active product (specification §11, §17, [D56]).
 *
 * What this product's assistant is allowed to answer from, and nothing else: another
 * product's documents are never retrieved here. A knowledge base with no segment is
 * shared across every segment of the product, which is the normal case.
 *
 * §17 asks for: create or select a knowledge base, upload documents, status, archive, and
 * ingestion errors. The last one matters most — a document that failed to ingest is
 * invisible to the assistant, and without the reason on screen nobody knows why an answer
 * was missing.
 *
 * Changing knowledge is for administrators (D-07): it is what the assistant tells
 * customers. Everyone can read it. The API enforces the rule; hiding the controls only
 * spares a salesperson a refusal.
 *
 * Knowledge is configuration, so an inactive product can still be prepared here.
 */

'use client';

import { useCallback, useEffect, useState } from 'react';

import { useProduct } from '@/components/product-context';
import { SegmentBadge } from '@/components/segment-filter';
import { StatusBadge } from '@/components/status-badge';
import { Alert, Button, Card, EmptyState, PageHeader } from '@/components/ui';
import type { KnowledgeBase, KnowledgeDocument } from '@/lib/api';

export default function KnowledgePage() {
  const { api, product, user } = useProduct();
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [selected, setSelected] = useState<string>('');
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [newBaseName, setNewBaseName] = useState('');
  const [newBaseSegment, setNewBaseSegment] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const isAdmin = user?.role === 'ADMIN';
  const segments = product.segments.filter((segment) => segment.is_active);

  const loadBases = useCallback(() => {
    api
      .listKnowledgeBases()
      .then((result) => {
        setBases(result);
        setSelected((current) =>
          result.some((base) => base.id === current) ? current : (result[0]?.id ?? ''),
        );
      })
      .catch((e: Error) => setError(e.message));
  }, [api]);

  const loadDocuments = useCallback(() => {
    if (!selected) {
      setDocuments([]);
      return;
    }
    api
      .listDocuments(selected, includeArchived)
      .then(setDocuments)
      .catch((e: Error) => setError(e.message));
  }, [api, selected, includeArchived]);

  useEffect(loadBases, [loadBases]);
  useEffect(loadDocuments, [loadDocuments]);

  async function createBase(event: React.FormEvent) {
    event.preventDefault();
    if (!newBaseName.trim()) {
      return;
    }
    setError(null);
    try {
      const created = await api.createKnowledgeBase(
        newBaseName.trim(),
        null,
        newBaseSegment || null,
      );
      setNewBaseName('');
      setNewBaseSegment('');
      setSelected(created.id);
      loadBases();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function upload(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file || !selected) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.uploadDocument(selected, file);
      loadDocuments();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      // Clear the input so uploading the same file again still fires a change event.
      event.target.value = '';
    }
  }

  async function archive(documentId: string) {
    setError(null);
    try {
      await api.archiveDocument(selected, documentId);
      loadDocuments();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function remove(documentId: string, title: string) {
    const confirmed = window.confirm(
      `Permanently delete "${title}"? Its chunks stop being retrievable, and past assistant replies that cited them will keep references that no longer resolve. This cannot be undone.`,
    );
    if (!confirmed) {
      return;
    }
    setError(null);
    try {
      await api.deleteDocument(selected, documentId);
      loadDocuments();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const pending = documents.some(
    (document) => document.status === 'PENDING' || document.status === 'PROCESSING',
  );

  return (
    <>
      <PageHeader
        title="Knowledge base"
        description={`What ${product.display_name}'s assistant is allowed to answer from. Only READY documents are used, and no other product's documents are ever retrieved.`}
      />

      {error ? <Alert className="mb-6">{error}</Alert> : null}

      {user && !isAdmin ? (
        <Alert tone="info" className="mb-6">
          Only administrators can add, upload, archive or delete knowledge, because it is
          what the assistant tells customers.
        </Alert>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,16rem)_minmax(0,1fr)]">
        <Card>
          <h2 className="text-sm font-semibold">Collections</h2>
          {bases.length === 0 ? (
            <p className="mt-3 text-sm text-ink-muted">None yet.</p>
          ) : (
            <ul className="mt-3 space-y-1">
              {bases.map((base) => (
                <li key={base.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(base.id)}
                    className={
                      selected === base.id
                        ? 'w-full rounded-lg bg-ink px-3 py-2 text-left text-sm font-medium text-white'
                        : 'w-full rounded-lg px-3 py-2 text-left text-sm text-ink-muted hover:bg-canvas hover:text-ink'
                    }
                  >
                    <span className="block truncate">{base.name}</span>
                    <span className="mt-0.5 block text-[11px] opacity-70">
                      {base.segment_id ? 'One segment' : 'Shared across segments'}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}

          {isAdmin ? (
            <form onSubmit={createBase} className="mt-5 space-y-2 border-t border-line pt-4">
              <label htmlFor="new-kb" className="block text-xs font-medium text-ink-muted">
                New knowledge base
              </label>
              <input
                id="new-kb"
                value={newBaseName}
                onChange={(event) => setNewBaseName(event.target.value)}
                placeholder="Product FAQs"
                className="ui-input"
              />
              {segments.length > 0 ? (
                <select
                  aria-label="Segment"
                  value={newBaseSegment}
                  onChange={(event) => setNewBaseSegment(event.target.value)}
                  className="ui-select"
                >
                  <option value="">Shared across segments</option>
                  {segments.map((segment) => (
                    <option key={segment.id} value={segment.id}>
                      Only {segment.display_name}
                    </option>
                  ))}
                </select>
              ) : null}
              <Button type="submit" variant="secondary" className="w-full">
                Create
              </Button>
            </form>
          ) : null}
        </Card>

        <div className="space-y-4">
          {selected && isAdmin ? (
            <Card>
              <div className="flex flex-wrap items-center gap-2">
                <label htmlFor="upload" className="block text-sm font-semibold">
                  Upload a document
                </label>
                <SegmentBadge
                  segmentId={bases.find((base) => base.id === selected)?.segment_id ?? null}
                />
              </div>
              <p className="mt-1 text-xs text-ink-muted">
                .txt, .md or .pdf. Ingestion runs in the background, so a new document starts as
                PENDING.
              </p>
              <label
                htmlFor="upload"
                className="mt-3 flex cursor-pointer items-center justify-between gap-3 rounded-lg border border-dashed border-line bg-canvas px-4 py-3 text-sm"
              >
                <span className="text-ink-muted">
                  {busy ? 'Uploading…' : 'Choose a file to ingest'}
                </span>
                <span className="rounded-md bg-white px-2 py-1 text-xs font-medium text-ink shadow-sm">
                  Browse
                </span>
              </label>
              <input
                id="upload"
                type="file"
                accept=".txt,.md,.pdf"
                onChange={upload}
                disabled={busy}
                className="sr-only"
              />
            </Card>
          ) : null}

          <div className="flex items-center justify-between gap-3">
            <h2 className="text-sm font-semibold">Documents</h2>
            <div className="flex items-center gap-3 text-sm">
              <label className="flex items-center gap-2 text-ink-muted">
                <input
                  type="checkbox"
                  checked={includeArchived}
                  onChange={(event) => setIncludeArchived(event.target.checked)}
                  className="rounded border-line"
                />
                Show archived
              </label>
              {pending ? (
                <button
                  type="button"
                  onClick={loadDocuments}
                  className="font-medium text-forest hover:underline"
                >
                  Refresh
                </button>
              ) : null}
            </div>
          </div>

          {documents.length === 0 ? (
            <EmptyState
              title="No documents yet"
              description="Only READY documents are retrieved. A failed ingestion shows its error here so a missing answer is not a mystery."
            />
          ) : (
            <ul className="space-y-2">
              {documents.map((document) => (
                <li
                  key={document.id}
                  className="rounded-2xl border border-line bg-white p-4 shadow-card"
                >
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <p className="font-medium">{document.title}</p>
                      <p className="mt-0.5 text-xs text-ink-muted">
                        {document.chunk_count} chunk
                        {document.chunk_count === 1 ? '' : 's'}
                        {document.byte_size ? ` · ${Math.ceil(document.byte_size / 1024)} KB` : ''}
                      </p>
                      {document.error_message ? (
                        <p className="mt-1 text-sm text-red-800">{document.error_message}</p>
                      ) : null}
                    </div>
                    <div className="flex shrink-0 items-center gap-3">
                      <StatusBadge status={document.status} />
                      {document.status === 'ARCHIVED' || !isAdmin ? null : (
                        <button
                          type="button"
                          onClick={() => archive(document.id)}
                          className="text-sm text-ink-muted hover:text-ink"
                        >
                          Archive
                        </button>
                      )}
                      {!isAdmin ||
                      document.status === 'PENDING' ||
                      document.status === 'PROCESSING' ? null : (
                        <button
                          type="button"
                          onClick={() => remove(document.id, document.title)}
                          className="text-sm text-red-700 hover:text-red-900"
                          title="Permanently delete this document and its chunks"
                        >
                          Delete
                        </button>
                      )}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </>
  );
}
