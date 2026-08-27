import type { ComponentType, SVGProps } from 'react';

type IconProps = SVGProps<SVGSVGElement>;

function Svg({ children, className, ...props }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
      {...props}
    >
      {children}
    </svg>
  );
}

export function IconOverview(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="3" y="3" width="7" height="7" rx="1.5" />
      <rect x="14" y="3" width="7" height="7" rx="1.5" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" />
      <rect x="14" y="14" width="7" height="7" rx="1.5" />
    </Svg>
  );
}

export function IconAudiences(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M22 21v-2a4 4 0 0 0-3-3.87" />
      <path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </Svg>
  );
}

export function IconCampaigns(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m3 11 19-8-8 19-2.7-7.3L3 11z" />
    </Svg>
  );
}

export function IconLeads(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M8 6h13" />
      <path d="M8 12h13" />
      <path d="M8 18h13" />
      <path d="M3 6h.01" />
      <path d="M3 12h.01" />
      <path d="M3 18h.01" />
    </Svg>
  );
}

export function IconConversations(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z" />
    </Svg>
  );
}

export function IconBookings(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="3" y="5" width="18" height="16" rx="2" />
      <path d="M16 3v4" />
      <path d="M8 3v4" />
      <path d="M3 11h18" />
    </Svg>
  );
}

export function IconKnowledge(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
      <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
    </Svg>
  );
}

export function IconNotifications(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
      <path d="M10.3 21a1.7 1.7 0 0 0 3.4 0" />
    </Svg>
  );
}

export function IconMenu(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4 7h16" />
      <path d="M4 12h16" />
      <path d="M4 17h16" />
    </Svg>
  );
}

export function IconClose(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M18 6 6 18" />
      <path d="m6 6 12 12" />
    </Svg>
  );
}

export function IconCheck(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m5 12.5 4.5 4.5L19 7.5" />
    </Svg>
  );
}

export function IconChecks(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m2.5 12.5 4.5 4.5L16 8" />
      <path d="m11 16 1.5 1.5L21.5 8.5" />
    </Svg>
  );
}

export function IconArrowUpRight(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M7 17 17 7" />
      <path d="M8 7h9v9" />
    </Svg>
  );
}

export function IconChevronRight(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m9 6 6 6-6 6" />
    </Svg>
  );
}

export function IconSparkles(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 4.5 13.7 9 18 10.5 13.7 12 12 16.5 10.3 12 6 10.5 10.3 9 12 4.5z" />
      <path d="M18.5 14.5 19.3 16.6 21.5 17.4 19.3 18.2 18.5 20.3 17.7 18.2 15.5 17.4 17.7 16.6 18.5 14.5z" />
    </Svg>
  );
}

export function IconMark(props: IconProps) {
  return (
    <svg viewBox="0 0 32 32" fill="none" aria-hidden="true" {...props}>
      <defs>
        <linearGradient id="icon-mark-fill" x1="0" y1="0" x2="32" y2="32">
          <stop offset="0" stopColor="#10B981" />
          <stop offset="1" stopColor="#047A57" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="9" fill="url(#icon-mark-fill)" />
      <path
        d="M9 16.2c2.2-1.1 4.4.4 7 2.6 2.1-4.4 5.2-7.4 8-8.8"
        stroke="white"
        strokeWidth="2.2"
        strokeLinecap="round"
      />
    </svg>
  );
}

export const NAV_ICONS: Record<string, ComponentType<IconProps>> = {
  '/': IconOverview,
  '/audiences': IconAudiences,
  '/campaigns': IconCampaigns,
  '/leads': IconLeads,
  '/conversations': IconConversations,
  '/bookings': IconBookings,
  '/knowledge': IconKnowledge,
  '/notifications': IconNotifications,
};
