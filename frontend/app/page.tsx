/**
 * All products (§28, [D53]).
 *
 * The landing page. It chooses no product for the operator: the product is always an
 * explicit choice, so this lists them — from the API, however many there are — and shows
 * the organisation's combined figures beneath, clearly labelled as combined.
 */

'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';

import { OverviewMetrics } from '@/components/overview-metrics';
import { AppShell } from '@/components/shell';
import { Alert, EmptyState, PageHeader, SectionLabel, Skeleton } from '@/components/ui';
import { api, type Product } from '@/lib/api';
import { productHref } from '@/lib/product-routes';

export default function AllProductsPage() {
  const [products, setProducts] = useState<Product[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listProducts()
      .then(setProducts)
      .catch((e: Error) => setError(e.message));
  }, []);

  const loadCombined = useCallback(
    () => Promise.all([api.getCombinedOverview(), api.getCombinedLeadFunnel()]),
    [],
  );

  const active = (products ?? []).filter((product) => product.is_active);
  const inactive = (products ?? []).filter((product) => !product.is_active);

  return (
    <AppShell>
      <PageHeader
        title="Products"
        description="Choose the product to work in. Audiences, campaigns, leads, conversations, demos and knowledge all belong to one product."
      />

      {error ? <Alert className="mb-6">{error}</Alert> : null}

      {products === null && !error ? (
        <Skeleton className="h-32" />
      ) : products !== null && products.length === 0 ? (
        <EmptyState
          title="No products are configured"
          description="Products are configuration, not code. Once one exists it appears here."
        />
      ) : (
        <div className="space-y-6">
          <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {active.map((product) => (
              <li key={product.id}>
                <Link
                  href={productHref(product.slug)}
                  className="block rounded-2xl border border-line bg-white p-5 shadow-card transition hover:-translate-y-0.5 hover:border-forest/30 hover:shadow-lift"
                >
                  <p className="text-lg font-extrabold tracking-tight text-ink">
                    {product.display_name}
                  </p>
                  <p className="mt-1 font-mono text-xs text-ink-muted">/p/{product.slug}</p>
                </Link>
              </li>
            ))}
          </ul>

          {inactive.length > 0 ? (
            <div>
              <SectionLabel>Inactive</SectionLabel>
              <ul className="mt-3 flex flex-wrap gap-2">
                {inactive.map((product) => (
                  <li key={product.id}>
                    <Link
                      href={productHref(product.slug)}
                      className="inline-flex rounded-full border border-line bg-white px-3 py-1.5 text-sm text-ink-muted hover:text-ink"
                    >
                      {product.display_name}
                    </Link>
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-xs text-ink-muted">
                Readable for history. New customer activity is refused.
              </p>
            </div>
          ) : null}
        </div>
      )}

      <section className="mt-12">
        <div className="mb-4">
          <SectionLabel>All products combined</SectionLabel>
          <p className="mt-1 text-sm text-ink-muted">
            Every product added together. A person who is a lead in two products counts twice,
            because they are two leads.
          </p>
        </div>
        <OverviewMetrics load={loadCombined} />
      </section>
    </AppShell>
  );
}
