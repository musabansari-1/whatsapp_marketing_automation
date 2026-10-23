import { IconMark } from '@/components/icons';
import { Skeleton } from '@/components/ui';

/**
 * The shell's outline while a product is still loading: the sidebar and header keep
 * their place, so a slow load reads as "loading", not as a blank or broken page.
 */
export function ShellSkeleton() {
  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[17rem_minmax(0,1fr)]">
      <aside className="sticky top-0 hidden h-screen bg-sidebar lg:block">
        <div className="flex items-center gap-3 px-5 pb-5 pt-6">
          <IconMark className="size-9 shrink-0" />
          <div className="space-y-1.5">
            <div className="h-3 w-24 rounded bg-sidebar-hover" />
            <div className="h-2.5 w-20 rounded bg-sidebar-hover" />
          </div>
        </div>
        <div className="space-y-2 px-3">
          {Array.from({ length: 8 }, (_, i) => (
            <div key={i} className="h-8 animate-pulse rounded-xl bg-sidebar-hover/70" />
          ))}
        </div>
      </aside>
      <div className="flex min-h-screen min-w-0 flex-col">
        <header className="h-16 border-b border-line bg-canvas/80" />
        <main className="mx-auto w-full max-w-6xl flex-1 space-y-4 px-4 py-8 sm:px-8 sm:py-10">
          <Skeleton className="h-9 w-56" />
          <Skeleton className="h-64" />
        </main>
      </div>
    </div>
  );
}
