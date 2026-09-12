/**
 * Product workspace (§28, Phase 26).
 *
 * Every page under `/p/{slug}` renders inside the active product: its context is loaded
 * once here, and the shell shows which product is active on every page — so nobody
 * prepares or sends a campaign without the product in front of them.
 */

'use client';

import { use, type ReactNode } from 'react';

import { ProductProvider } from '@/components/product-context';
import { AppShell } from '@/components/shell';

export default function ProductLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ productSlug: string }>;
}) {
  const { productSlug } = use(params);
  return (
    <ProductProvider slug={decodeURIComponent(productSlug)}>
      <AppShell>{children}</AppShell>
    </ProductProvider>
  );
}
