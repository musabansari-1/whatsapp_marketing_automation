/**
 * Overview (specification §17).
 *
 * The first of the nine §17 sections, and the landing page: the numbers that answer
 * "is this working?" without opening anything else.
 *
 * Server-rendered down to the session check, so an unauthenticated visitor never sees
 * a flash of dashboard before the redirect. The metrics themselves load client-side
 * through the BFF proxy ([D13]).
 */

import { LogoutButton } from '@/app/logout-button';
import { Nav } from '@/app/nav';
import { OverviewMetrics } from '@/app/overview';
import { getCurrentUser } from '@/lib/session';

// Reflects live session state, so it must not be prerendered at build time.
export const dynamic = 'force-dynamic';

export default async function OverviewPage() {
  const user = await getCurrentUser();

  return (
    <>
      <Nav />
      <main className="mx-auto max-w-5xl px-6 py-10">
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Overview</h1>
            {user ? (
              <p className="mt-1 text-sm text-slate-600">
                {user.email} — <span className="font-mono text-xs">{user.role}</span>
              </p>
            ) : null}
          </div>
          {user ? <LogoutButton /> : null}
        </div>

        <div className="mt-8">
          <OverviewMetrics />
        </div>
      </main>
    </>
  );
}
