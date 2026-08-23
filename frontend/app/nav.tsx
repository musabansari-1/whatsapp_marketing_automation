/**
 * Dashboard navigation (specification §17).
 *
 * The nine §17 sections in one place, so a salesperson can follow a lead from an
 * uploaded audience through to a booked demo without knowing any URLs.
 *
 * A client component because it highlights the current path. It carries no data of
 * its own — the unread badge is passed in, so this file does not fetch on every
 * navigation. The section list and the active-path rule live in `lib/nav.ts`, where
 * they can be tested without rendering.
 */

'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

import { isActiveSection, NAV_SECTIONS } from '@/lib/nav';

export function Nav({ unread = 0 }: { unread?: number }) {
  const pathname = usePathname();

  return (
    <nav className="flex flex-wrap items-center gap-1 border-b border-slate-200 bg-white px-6 py-3">
      {NAV_SECTIONS.map((section) => (
        <Link
          key={section.href}
          href={section.href}
          className={`rounded px-3 py-1.5 text-sm ${
            isActiveSection(pathname, section.href)
              ? 'bg-slate-900 text-white'
              : 'text-slate-700 hover:bg-slate-100'
          }`}
        >
          {section.label}
          {section.href === '/notifications' && unread > 0 ? (
            <span className="ml-2 rounded-full bg-emerald-600 px-1.5 py-0.5 text-xs text-white">
              {unread}
            </span>
          ) : null}
        </Link>
      ))}
    </nav>
  );
}
