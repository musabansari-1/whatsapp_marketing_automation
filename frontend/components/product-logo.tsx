/**
 * A product's logo, or its initials when it has none yet.
 *
 * Logos live in `public/brand/{slug}.png` (the brands' official square icons, the same
 * files GlobalPact's MIS Tracker uses); add a file and its slug below to give a product
 * one. The initials tile takes a steady colour from the slug, so a product
 * always looks the same wherever it appears.
 */

import { cx } from '@/lib/cx';

const LOGOS: Record<string, string> = {
  'life-herba': '/brand/life-herba.png',
  sustainx: '/brand/sustainx.png',
  academicae: '/brand/academicae.png',
};

const TILE_COLOURS = [
  'bg-emerald-100 text-emerald-800',
  'bg-sky-100 text-sky-800',
  'bg-amber-100 text-amber-800',
  'bg-violet-100 text-violet-800',
  'bg-rose-100 text-rose-800',
];

function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  const [first, second] = words;
  const letters = first && second ? first.charAt(0) + second.charAt(0) : name.slice(0, 2);
  return letters.toUpperCase();
}

function colourFor(slug: string): string {
  let hash = 0;
  for (const char of slug) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return TILE_COLOURS[hash % TILE_COLOURS.length] ?? 'bg-stone-100 text-stone-700';
}

export function ProductLogo({
  slug,
  name,
  className,
}: {
  slug: string;
  name: string;
  className?: string;
}) {
  const src = LOGOS[slug];
  if (src) {
    return (
      // A plain img: the logos are tiny static files and need no image pipeline.
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={src}
        alt=""
        className={cx('shrink-0 rounded-lg bg-white object-contain ring-1 ring-black/5', className)}
      />
    );
  }
  return (
    <span
      aria-hidden="true"
      className={cx(
        'flex shrink-0 items-center justify-center rounded-lg text-[11px] font-bold',
        colourFor(slug),
        className,
      )}
    >
      {initials(name)}
    </span>
  );
}
