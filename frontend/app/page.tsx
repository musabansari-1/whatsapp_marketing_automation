/**
 * Foundation landing page.
 *
 * Deliberately minimal: the nine dashboard sections from specification §17 are
 * Phase 13. This page exists to prove the Phase 1 wiring — that the frontend renders
 * and that the BFF proxy can reach the API — and shows which phases are pending.
 */

import Link from 'next/link';

import { LogoutButton } from '@/app/logout-button';
import { getInternalApiBaseUrl } from '@/lib/bff';
import { getCurrentUser } from '@/lib/session';

// Reflects live API and session state, so it must not be prerendered at build time.
export const dynamic = 'force-dynamic';

type ApiStatus = { reachable: true; environment: string } | { reachable: false; detail: string };

async function fetchApiStatus(): Promise<ApiStatus> {
  try {
    const response = await fetch(`${getInternalApiBaseUrl()}/health`, { cache: 'no-store' });
    if (!response.ok) {
      return { reachable: false, detail: `API returned ${response.status}` };
    }
    const body = (await response.json()) as { environment?: string };
    return { reachable: true, environment: body.environment ?? 'unknown' };
  } catch (error) {
    return { reachable: false, detail: error instanceof Error ? error.message : String(error) };
  }
}

const PHASES = [
  { phase: 1, name: 'Foundation and abstractions', done: true },
  { phase: 2, name: 'Database and pgvector', done: true },
  { phase: 3, name: 'Authentication', done: true },
  { phase: 4, name: 'Excel audience', done: true },
  { phase: 5, name: 'Campaigns', done: true },
  { phase: 6, name: 'WhatsApp', done: false },
  { phase: 7, name: 'Cloud Tasks and worker', done: false },
  { phase: 8, name: 'RAG knowledge base', done: false },
  { phase: 9, name: 'Conversations', done: false },
  { phase: 10, name: 'AI agent', done: false },
  { phase: 11, name: 'Calendar, booking, notifications', done: false },
  { phase: 12, name: 'HubSpot', done: false },
  { phase: 13, name: 'Dashboard and analytics', done: false },
  { phase: 14, name: 'Hardening', done: false },
  { phase: 15, name: 'Production deployment', done: false },
];

export default async function HomePage() {
  const [status, user] = await Promise.all([fetchApiStatus(), getCurrentUser()]);

  return (
    <main className="mx-auto max-w-3xl px-6 py-16">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">WhatsApp AI Marketing Platform</h1>
          <p className="mt-2 text-sm text-slate-600">
            Internal platform. The dashboard arrives in Phase 13.
          </p>
        </div>
        {user && <LogoutButton />}
      </div>

      {user && (
        <section className="mt-8 rounded-lg border border-slate-200 bg-white p-5">
          <h2 className="text-sm font-medium text-slate-900">Signed in</h2>
          <p className="mt-2 text-sm text-slate-700">
            {user.email} — <span className="font-mono text-xs">{user.role}</span>
          </p>
          <div className="mt-3 flex gap-4">
            <Link href="/audiences" className="text-sm underline">
              Audiences
            </Link>
            <Link href="/campaigns" className="text-sm underline">
              Campaigns
            </Link>
          </div>
        </section>
      )}

      <section className="mt-8 rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="text-sm font-medium text-slate-900">API connectivity</h2>
        {status.reachable ? (
          <p className="mt-2 text-sm text-emerald-700">
            Reachable — environment <span className="font-mono">{status.environment}</span>
          </p>
        ) : (
          <p className="mt-2 text-sm text-amber-700">Not reachable — {status.detail}</p>
        )}
        <p className="mt-3 text-xs text-slate-500">
          Browser requests reach the API through this app&apos;s server-side proxy, never directly.
        </p>
      </section>

      <section className="mt-6 rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="text-sm font-medium text-slate-900">Implementation phases</h2>
        <ul className="mt-3 space-y-1">
          {PHASES.map(({ phase, name, done }) => (
            <li key={phase} className="flex items-baseline gap-3 text-sm">
              <span
                className={`w-16 shrink-0 font-mono text-xs ${done ? 'text-emerald-700' : 'text-slate-400'}`}
              >
                {done ? 'done' : `${phase}`}
              </span>
              <span className={done ? 'text-slate-900' : 'text-slate-500'}>{name}</span>
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
