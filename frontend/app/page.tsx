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

import { AppShell } from '@/components/shell';
import { PageHeader } from '@/components/ui';
import { OverviewMetrics } from '@/app/overview';

export const dynamic = 'force-dynamic';

export default function OverviewPage() {
  return (
    <AppShell>
      <PageHeader
        title="Overview"
        description="Reach, engagement and outcomes — enough to see whether the pipeline is moving without opening another page."
      />
      <OverviewMetrics />
    </AppShell>
  );
}
