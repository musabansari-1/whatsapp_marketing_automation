/**
 * The dashboard's icons: lucide (the same set LifeHerba's admin console uses), named
 * for what they mean here so pages never import a glyph by its picture.
 */

import {
  Bell,
  BookOpen,
  CalendarCheck,
  Check,
  CheckCheck,
  ChevronRight,
  FileText,
  LayoutDashboard,
  Layers,
  Menu,
  MessagesSquare,
  Send,
  Sparkles,
  SquareArrowOutUpRight,
  UserRound,
  Users,
  X,
  type LucideIcon,
} from 'lucide-react';
import type { ComponentType, SVGProps } from 'react';

type IconProps = SVGProps<SVGSVGElement>;

/** Lucide at the dashboard's stroke weight, hidden from screen readers by default. */
function icon(Glyph: LucideIcon): ComponentType<IconProps> {
  function DashboardIcon(props: IconProps) {
    return <Glyph strokeWidth={1.8} aria-hidden="true" {...(props as object)} />;
  }
  DashboardIcon.displayName = Glyph.displayName;
  return DashboardIcon;
}

export const IconOverview = icon(LayoutDashboard);
export const IconProducts = icon(Layers);
export const IconAudiences = icon(Users);
export const IconCampaigns = icon(Send);
export const IconTemplates = icon(FileText);
export const IconLeads = icon(UserRound);
export const IconConversations = icon(MessagesSquare);
export const IconBookings = icon(CalendarCheck);
export const IconKnowledge = icon(BookOpen);
export const IconNotifications = icon(Bell);
export const IconMenu = icon(Menu);
export const IconClose = icon(X);
export const IconCheck = icon(Check);
export const IconChecks = icon(CheckCheck);
export const IconArrowUpRight = icon(SquareArrowOutUpRight);
export const IconChevronRight = icon(ChevronRight);
export const IconSparkles = icon(Sparkles);

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

/** Section icons, keyed by `NavSection.key` rather than path, since paths are per product. */
export const NAV_ICONS: Record<string, ComponentType<IconProps>> = {
  overview: IconOverview,
  audiences: IconAudiences,
  campaigns: IconCampaigns,
  templates: IconTemplates,
  leads: IconLeads,
  conversations: IconConversations,
  bookings: IconBookings,
  knowledge: IconKnowledge,
  notifications: IconNotifications,
  products: IconProducts,
};
