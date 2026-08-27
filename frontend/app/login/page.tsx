/**
 * Login page.
 *
 * Posts to the BFF proxy at the frontend origin ([D13]), never to the API directly.
 * The API's Set-Cookie travels back through the proxy and the browser stores it as a
 * same-site cookie, which is what lets [D2]'s HttpOnly session cookie work across the
 * Vercel/Cloud Run split.
 */

'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { IconMark } from '@/components/icons';
import { Button } from '@/components/ui';

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);

    try {
      const response = await fetch('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ email, password }),
      });

      if (!response.ok) {
        // The API deliberately does not distinguish unknown-email from
        // wrong-password, and neither does this message.
        setError('Invalid email or password.');
        return;
      }

      router.replace('/');
      router.refresh();
    } catch {
      setError('Could not reach the server. Please try again.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="grid min-h-screen lg:grid-cols-[1.1fr_0.9fr]">
      <section className="relative hidden overflow-hidden bg-sidebar px-12 py-12 text-sidebar-fg lg:flex lg:flex-col lg:justify-between">
        <div
          className="pointer-events-none absolute inset-0 opacity-50"
          style={{
            backgroundImage:
              'radial-gradient(circle at 20% 15%, rgb(16 185 129 / 0.4), transparent 42%), radial-gradient(circle at 85% 90%, rgb(255 255 255 / 0.05), transparent 40%)',
          }}
        />
        <div className="pointer-events-none absolute -inset-x-20 -top-20 h-72 rounded-full bg-forest-bright/10 blur-3xl" />
        <div className="relative">
          <div className="flex items-center gap-3">
            <IconMark className="size-10 drop-shadow-[0_4px_16px_rgb(16_185_129/0.4)]" />
            <div>
              <p className="text-base font-bold tracking-tight">WhatsApp AI</p>
              <p className="text-xs text-sidebar-muted">Marketing platform</p>
            </div>
          </div>
        </div>
        <div className="relative max-w-md">
          <p className="text-4xl font-extrabold leading-[1.12] tracking-tight">
            From audience file to booked demo, in one console.
          </p>
          <p className="mt-5 text-sm leading-relaxed text-sidebar-muted">
            Upload a list. Broadcast an approved template. The assistant qualifies replies and books
            against the sales calendar.
          </p>
        </div>
        <div className="relative flex items-center gap-3 text-xs text-sidebar-muted">
          <span className="flex size-5 items-center justify-center rounded-full bg-forest-bright/20 text-forest-bright">
            ✓
          </span>
          Internal use. Accounts are created by an administrator.
        </div>
      </section>

      <section className="flex items-center justify-center bg-canvas px-6 py-16">
        <div className="w-full max-w-sm">
          <div className="mb-8 flex items-center gap-3 lg:hidden">
            <IconMark className="size-9" />
            <div>
              <p className="text-sm font-semibold">WhatsApp AI</p>
              <p className="text-xs text-ink-muted">Marketing platform</p>
            </div>
          </div>

          <h1 className="text-2xl font-extrabold tracking-tight text-ink">Sign in</h1>
          <p className="mt-1.5 text-sm text-ink-muted">
            Use the account an administrator created for you.
          </p>

          <form onSubmit={handleSubmit} className="mt-8 space-y-4">
            <div>
              <label htmlFor="email" className="block text-sm font-medium text-ink">
                Email
              </label>
              <input
                id="email"
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                className="ui-input mt-1.5"
              />
            </div>

            <div>
              <label htmlFor="password" className="block text-sm font-medium text-ink">
                Password
              </label>
              <input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className="ui-input mt-1.5"
              />
            </div>

            {error ? (
              <p role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800">
                {error}
              </p>
            ) : null}

            <Button type="submit" disabled={submitting} className="w-full py-2.5">
              {submitting ? 'Signing in…' : 'Sign in'}
            </Button>
          </form>

          <p className="mt-8 text-xs leading-relaxed text-ink-muted">
            There is no self-registration. If you cannot sign in, ask an administrator to create or
            reset your account.
          </p>
        </div>
      </section>
    </main>
  );
}
