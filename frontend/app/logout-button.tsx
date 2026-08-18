/**
 * Logout control.
 *
 * Posts to the BFF proxy, which forwards to the API. The API revokes the stored
 * session row as well as clearing the cookie, so the session genuinely ends rather
 * than merely disappearing from this browser.
 */

'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

export function LogoutButton() {
  const router = useRouter();
  const [pending, setPending] = useState(false);

  async function handleLogout() {
    setPending(true);
    try {
      await fetch('/api/v1/auth/logout', { method: 'POST', credentials: 'same-origin' });
      router.replace('/login');
      router.refresh();
    } finally {
      setPending(false);
    }
  }

  return (
    <button
      type="button"
      onClick={handleLogout}
      disabled={pending}
      className="shrink-0 rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-700 disabled:opacity-60"
    >
      {pending ? 'Signing out…' : 'Sign out'}
    </button>
  );
}
