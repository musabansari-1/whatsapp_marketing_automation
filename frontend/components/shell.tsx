/**
 * Dashboard chrome: sidebar, top bar, and a single content column.
 *
 * The nine §17 sections live in `lib/nav.ts`. This file only frames them, so a
 * missing section is still a specification gap rather than a layout bug.
 */

'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect, useMemo, useState, type ReactNode } from 'react';

import { LogoutButton } from '@/app/logout-button';
import {
  IconChevronRight,
  IconClose,
  IconMark,
  IconMenu,
  IconNotifications,
  NAV_ICONS,
} from '@/components/icons';
import { cx } from '@/lib/cx';
import { api, type SessionUser } from '@/lib/api';
import { isActiveSection, NAV_SECTIONS } from '@/lib/nav';

const GROUPS: { label: string | null; hrefs: readonly string[] }[] = [
  { label: null, hrefs: ['/'] },
  { label: 'Outreach', hrefs: ['/audiences', '/campaigns'] },
  { label: 'Pipeline', hrefs: ['/leads', '/conversations', '/bookings'] },
  { label: 'Workspace', hrefs: ['/knowledge', '/notifications'] },
];

const PAGE_TITLES: Record<string, string> = Object.fromEntries(
  NAV_SECTIONS.map((section) => [section.href, section.label]),
);

function titleFor(pathname: string): string {
  if (PAGE_TITLES[pathname]) {
    return PAGE_TITLES[pathname];
  }
  const match = NAV_SECTIONS.find(
    (section) => section.href !== '/' && pathname.startsWith(`${section.href}/`),
  );
  return match?.label ?? 'Console';
}

function NavLink({
  pathname,
  href,
  label,
  unread,
  onNavigate,
}: {
  pathname: string;
  href: string;
  label: string;
  unread: number;
  onNavigate: () => void;
}) {
  const active = isActiveSection(pathname, href);
  const Icon = NAV_ICONS[href];
  return (
    <Link
      href={href}
      onClick={onNavigate}
      aria-current={active ? 'page' : undefined}
      className={cx(
        'group relative flex items-center gap-2.5 rounded-xl px-3 py-2 text-[13px] transition-all duration-200 ease-out-expo',
        active
          ? 'bg-sidebar-active font-semibold text-white shadow-[inset_0_1px_0_rgb(255_255_255/0.06)]'
          : 'text-sidebar-muted hover:bg-sidebar-hover hover:text-sidebar-fg',
      )}
    >
      <span
        className={cx(
          'absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-full bg-forest-bright transition-all duration-200',
          active ? 'opacity-100' : 'opacity-0 group-hover:opacity-40',
        )}
      />
      {Icon ? (
        <Icon
          className={cx(
            'size-4 shrink-0 transition-colors',
            active ? 'text-forest-bright' : 'opacity-80 group-hover:opacity-100',
          )}
        />
      ) : null}
      <span className="flex-1 truncate">{label}</span>
      {href === '/notifications' && unread > 0 ? (
        <span className="min-w-5 rounded-full bg-forest px-1.5 py-0.5 text-center text-[10px] font-bold leading-none text-white shadow-glow-sm">
          {unread > 99 ? '99+' : unread}
        </span>
      ) : null}
    </Link>
  );
}

function SidebarNav({
  pathname,
  unread,
  user,
  initials,
  onNavigate,
}: {
  pathname: string;
  unread: number;
  user: SessionUser | null;
  initials: string;
  onNavigate: () => void;
}) {
  return (
    <div className="relative flex h-full flex-col">
      <div
        className="pointer-events-none absolute inset-x-0 top-0 h-44"
        style={{
          backgroundImage:
            'radial-gradient(320px 160px at 30% -20%, rgb(16 185 129 / 0.16), transparent 70%)',
        }}
      />

      <div className="relative flex items-center gap-3 px-5 pb-5 pt-6">
        <IconMark className="size-9 shrink-0 drop-shadow-[0_4px_12px_rgb(16_185_129/0.35)]" />
        <div className="min-w-0">
          <p className="truncate text-sm font-bold tracking-tight text-sidebar-fg">WhatsApp AI</p>
          <p className="truncate text-[11px] text-sidebar-muted">Marketing console</p>
        </div>
      </div>

      <nav className="relative flex-1 space-y-6 overflow-y-auto px-3 pb-6">
        {GROUPS.map((group) => {
          const sections = NAV_SECTIONS.filter((section) => group.hrefs.includes(section.href));
          if (sections.length === 0) {
            return null;
          }
          return (
            <div key={group.label ?? 'home'}>
              {group.label ? (
                <p className="mb-2 px-3 text-[10px] font-bold uppercase tracking-[0.18em] text-sidebar-muted/70">
                  {group.label}
                </p>
              ) : null}
              <ul className="space-y-0.5">
                {sections.map((section) => (
                  <li key={section.href}>
                    <NavLink
                      pathname={pathname}
                      href={section.href}
                      label={section.label}
                      unread={unread}
                      onNavigate={onNavigate}
                    />
                  </li>
                ))}
              </ul>
            </div>
          );
        })}
        <div className="space-y-0.5">
          {NAV_SECTIONS.filter(
            (section) => !GROUPS.some((group) => group.hrefs.includes(section.href)),
          ).map((section) => (
            <NavLink
              key={section.href}
              pathname={pathname}
              href={section.href}
              label={section.label}
              unread={unread}
              onNavigate={onNavigate}
            />
          ))}
        </div>
      </nav>

      <div className="relative border-t border-sidebar-line px-3 py-3">
        <div className="flex items-center gap-2.5 rounded-xl px-2 py-1.5">
          <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-forest-bright to-forest text-[11px] font-bold text-white shadow-glow-sm">
            {initials || '•'}
          </span>
          <div className="min-w-0 flex-1">
            <p className="truncate text-xs font-semibold text-sidebar-fg">
              {user ? user.full_name || user.email : 'Signed in'}
            </p>
            {user ? (
              <p className="font-mono text-[10px] uppercase tracking-wider text-sidebar-muted">
                {user.role}
              </p>
            ) : null}
          </div>
          <LogoutButton />
        </div>
      </div>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const [user, setUser] = useState<SessionUser | null>(null);
  const [unread, setUnread] = useState(0);

  useEffect(() => {
    api
      .getMe()
      .then(setUser)
      .catch(() => setUser(null));
    api
      .listNotifications(false, 1, 0)
      .then((page) => setUnread(page.unread))
      .catch(() => setUnread(0));
  }, [pathname]);

  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  const title = useMemo(() => titleFor(pathname), [pathname]);
  const initials = (user?.full_name || user?.email || '?')
    .split(/[\s@]/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? '')
    .join('');

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[17rem_minmax(0,1fr)]">
      <aside className="sticky top-0 hidden h-screen bg-sidebar lg:block">
        <SidebarNav
          pathname={pathname}
          unread={unread}
          user={user}
          initials={initials}
          onNavigate={() => undefined}
        />
      </aside>

      {open ? (
        <div className="fixed inset-0 z-40 lg:hidden">
          <button
            type="button"
            aria-label="Close menu"
            className="absolute inset-0 animate-fade-in bg-ink/50 backdrop-blur-sm"
            onClick={() => setOpen(false)}
          />
          <aside className="relative z-10 h-full w-[17rem] bg-sidebar shadow-lift">
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="absolute right-3 top-4 z-20 rounded-lg p-1.5 text-sidebar-muted transition hover:bg-sidebar-hover hover:text-sidebar-fg"
              aria-label="Close menu"
            >
              <IconClose className="size-4" />
            </button>
            <SidebarNav
              pathname={pathname}
              unread={unread}
              user={user}
              initials={initials}
              onNavigate={() => setOpen(false)}
            />
          </aside>
        </div>
      ) : null}

      <div className="flex min-h-screen min-w-0 flex-col">
        <header className="sticky top-0 z-30 flex h-16 items-center justify-between gap-3 border-b border-line bg-canvas/80 px-4 backdrop-blur-xl sm:px-8">
          <div className="flex min-w-0 items-center gap-3">
            <button
              type="button"
              className="rounded-lg p-2 text-ink-muted transition hover:bg-white hover:text-ink hover:shadow-input lg:hidden"
              onClick={() => setOpen(true)}
              aria-label="Open menu"
            >
              <IconMenu className="size-5" />
            </button>
            <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1.5 text-sm">
              <span className="hidden font-medium text-ink-faint sm:inline">Console</span>
              <IconChevronRight className="hidden size-3.5 shrink-0 text-ink-faint sm:block" />
              <span className="truncate font-semibold text-ink">{title}</span>
            </nav>
          </div>

          <div className="flex items-center gap-2">
            <Link
              href="/notifications"
              aria-label={unread > 0 ? `Notifications, ${unread} unread` : 'Notifications'}
              className="relative rounded-xl border border-line bg-white p-2 text-ink-muted shadow-input transition hover:border-ink/15 hover:text-ink"
            >
              <IconNotifications className="size-4" />
              {unread > 0 ? (
                <span className="absolute -right-1 -top-1 flex size-4 items-center justify-center rounded-full bg-forest text-[9px] font-bold text-white ring-2 ring-canvas">
                  {unread > 9 ? '9+' : unread}
                </span>
              ) : null}
            </Link>
            {user ? (
              <div className="hidden items-center gap-2.5 rounded-xl border border-line bg-white py-1.5 pl-1.5 pr-3 shadow-input sm:flex">
                <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-forest-bright to-forest text-[10px] font-bold text-white">
                  {initials || '•'}
                </span>
                <div className="min-w-0">
                  <p className="max-w-36 truncate text-xs font-semibold leading-tight text-ink">
                    {user.full_name || user.email}
                  </p>
                  <p className="font-mono text-[9px] uppercase leading-tight tracking-wider text-ink-muted">
                    {user.role}
                  </p>
                </div>
              </div>
            ) : null}
          </div>
        </header>

        <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-8 sm:py-10">
          <div className="animate-fade-up">{children}</div>
        </main>
      </div>
    </div>
  );
}
