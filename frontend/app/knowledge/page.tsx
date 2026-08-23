/**
 * Knowledge base (specification §17, §11).
 *
 * §17 asks for: create or select a knowledge base, upload documents, status, archive,
 * and ingestion errors. The last one matters most — a document that failed to ingest
 * is invisible to the assistant, and without the reason on screen nobody knows why an
 * answer was missing.
 *
 * Ingestion is asynchronous (§11), so an uploaded document appears as PENDING and this
 * page offers a refresh rather than pretending it is ready.
 */

'use client';

import { useCallback, useEffect, useState } from 'react';

import { Nav } from '@/app/nav';
import { api, type DocumentStatus, type KnowledgeBase, type KnowledgeDocument } from '@/lib/api';

const STATUS_STYLES: Record<DocumentStatus, string> = {
  READY: 'bg-emerald-100 text-emerald-800',
  PENDING: 'bg-slate-100 text-slate-700',
  PROCESSING: 'bg-sky-100 text-sky-800',
  FAILED: 'bg-red-100 text-red-800',
  ARCHIVED: 'bg-slate-200 text-slate-600',
};

export default function KnowledgePage() {
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [selected, setSelected] = useState<string>('');
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [newBaseName, setNewBaseName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadBases = useCallback(() => {
    api
      .listKnowledgeBases()
      .then((result) => {
        setBases(result);
        setSelected((current) => current || (result[0]?.id ?? ''));
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const loadDocuments = useCallback(() => {
    if (!selected) {
      setDocuments([]);
      return;
    }
    api
      .listDocuments(selected, includeArchived)
      .then(setDocuments)
      .catch((e: Error) => setError(e.message));
  }, [selected, includeArchived]);

  useEffect(loadBases, [loadBases]);
  useEffect(loadDocuments, [loadDocuments]);

  async function createBase(event: React.FormEvent) {
    event.preventDefault();
    if (!newBaseName.trim()) {
      return;
    }
    setError(null);
    try {
      const created = await api.createKnowledgeBase(newBaseName.trim(), null);
      setNewBaseName('');
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

  const pending = documents.some(
    (document) => document.status === 'PENDING' || document.status === 'PROCESSING',
  );

  return (
    <>
      <Nav />
      <main className="mx-auto max-w-4xl px-6 py-10">
        <h1 className="text-xl font-semibold tracking-tight">Knowledge base</h1>
        <p className="mt-1 text-sm text-slate-600">
          What the assistant is allowed to answer from. Only READY documents are used.
        </p>

        {error ? (
          <p className="mt-5 rounded border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            {error}
          </p>
        ) : null}

        <section className="mt-6 flex flex-wrap items-end gap-4">
          <div>
            <label htmlFor="kb" className="block text-sm text-slate-600">
              Knowledge base
            </label>
            <select
              id="kb"
              value={selected}
              onChange={(event) => setSelected(event.target.value)}
              className="mt-1 rounded border border-slate-300 px-3 py-2 text-sm"
            >
              {bases.length === 0 ? <option value="">None yet</option> : null}
              {bases.map((base) => (
                <option key={base.id} value={base.id}>
                  {base.name}
                </option>
              ))}
            </select>
          </div>

          <form onSubmit={createBase} className="flex items-end gap-2">
            <div>
              <label htmlFor="new-kb" className="block text-sm text-slate-600">
                New knowledge base
              </label>
              <input
                id="new-kb"
                value={newBaseName}
                onChange={(event) => setNewBaseName(event.target.value)}
                placeholder="Product FAQs"
                className="mt-1 rounded border border-slate-300 px-3 py-2 text-sm"
              />
            </div>
            <button
              type="submit"
              className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white"
            >
              Create
            </button>
          </form>
        </section>

        {selected ? (
          <section className="mt-6 rounded border border-slate-200 bg-white p-4">
            <label htmlFor="upload" className="block text-sm font-medium">
              Upload a document
            </label>
            <p className="mt-1 text-xs text-slate-500">
              .txt, .md or .pdf. Ingestion runs in the background, so a new document starts as
              PENDING.
            </p>
            <input
              id="upload"
              type="file"
              accept=".txt,.md,.pdf"
              onChange={upload}
              disabled={busy}
              className="mt-2 text-sm"
            />
          </section>
        ) : null}

        <section className="mt-6">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium">Documents</h2>
            <div className="flex items-center gap-3 text-sm">
              <label className="flex items-center gap-2 text-slate-600">
                <input
                  type="checkbox"
                  checked={includeArchived}
                  onChange={(event) => setIncludeArchived(event.target.checked)}
                />
                Show archived
              </label>
              {pending ? (
                <button type="button" onClick={loadDocuments} className="underline">
                  Refresh
                </button>
              ) : null}
            </div>
          </div>

          {documents.length === 0 ? (
            <p className="mt-2 rounded border border-slate-200 bg-white p-6 text-sm text-slate-600">
              No documents yet.
            </p>
          ) : (
            <ul className="mt-2 space-y-2">
              {documents.map((document) => (
                <li key={document.id} className="rounded border border-slate-200 bg-white p-4">
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <p className="font-medium">{document.title}</p>
                      <p className="text-xs text-slate-500">
                        {document.chunk_count} chunk
                        {document.chunk_count === 1 ? '' : 's'}
                        {document.byte_size ? ` · ${Math.ceil(document.byte_size / 1024)} KB` : ''}
                      </p>
                      {document.error_message ? (
                        <p className="mt-1 text-sm text-red-700">{document.error_message}</p>
                      ) : null}
                    </div>
                    <div className="flex shrink-0 items-center gap-3">
                      <span
                        className={`rounded px-2 py-1 text-xs font-medium ${
                          STATUS_STYLES[document.status]
                        }`}
                      >
                        {document.status}
                      </span>
                      {document.status === 'ARCHIVED' ? null : (
                        <button
                          type="button"
                          onClick={() => archive(document.id)}
                          className="text-sm text-slate-600 underline"
                        >
                          Archive
                        </button>
                      )}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>
    </>
  );
}
