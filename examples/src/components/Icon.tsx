// Tiny dependency-free icon set (Lucide-style 24px stroke geometry). Inline SVG
// keeps placeholders code-driven — no image assets, no extra packages.
import type { SVGProps } from 'react';

export type IconName =
  | 'search'
  | 'pin'
  | 'music'
  | 'glass'
  | 'sparkle'
  | 'calendar'
  | 'compass'
  | 'leaf'
  | 'moon'
  | 'fish'
  | 'star'
  | 'arrow';

const PATHS: Record<IconName, JSX.Element> = {
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="m21 21-4.3-4.3" />
    </>
  ),
  pin: (
    <>
      <path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z" />
      <circle cx="12" cy="10" r="3" />
    </>
  ),
  music: (
    <>
      <path d="M9 18V5l12-2v13" />
      <circle cx="6" cy="18" r="3" />
      <circle cx="18" cy="16" r="3" />
    </>
  ),
  glass: (
    <>
      <path d="M5 3h14l-1.5 9a5.5 5.5 0 0 1-11 0L5 3Z" />
      <path d="M12 17v4M8 21h8" />
    </>
  ),
  sparkle: (
    <>
      <path d="M12 3v4M12 17v4M3 12h4M17 12h4" />
      <path d="m6.3 6.3 2.4 2.4M15.3 15.3l2.4 2.4M17.7 6.3l-2.4 2.4M8.7 15.3l-2.4 2.4" />
    </>
  ),
  calendar: (
    <>
      <rect x="3" y="4" width="18" height="17" rx="2" />
      <path d="M3 9h18M8 2v4M16 2v4" />
    </>
  ),
  compass: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="m15.5 8.5-2 5-5 2 2-5 5-2Z" />
    </>
  ),
  leaf: (
    <>
      <path d="M11 20A7 7 0 0 1 4 13c0-5 5-9 16-9 0 9-4 14-9 14Z" />
      <path d="M9 16c2-4 5-6 9-7" />
    </>
  ),
  moon: <path d="M21 12.8A8 8 0 1 1 11.2 3a6.5 6.5 0 0 0 9.8 9.8Z" />,
  fish: (
    <>
      <path d="M2 12c3-5 9-7 14-5 3 1.2 5 3.5 6 5-1 1.5-3 3.8-6 5-5 2-11 0-14-5Z" />
      <circle cx="16" cy="11" r="0.6" fill="currentColor" />
    </>
  ),
  star: <path d="m12 3 2.6 5.6 6 .7-4.4 4 1.2 6-5.4-3-5.4 3 1.2-6-4.4-4 6-.7L12 3Z" />,
  arrow: <path d="M5 12h14M13 6l6 6-6 6" />,
};

interface Props extends Omit<SVGProps<SVGSVGElement>, 'name'> {
  name: IconName;
  size?: number;
}

export default function Icon({ name, size = 18, ...rest }: Props) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {PATHS[name]}
    </svg>
  );
}
