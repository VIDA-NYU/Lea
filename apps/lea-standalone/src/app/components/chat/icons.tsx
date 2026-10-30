import type { ReactNode, SVGProps } from 'react';

// Inline 24-unit stroke icons from the mockup artboards (1.7 stroke, round caps).
// Decorative: every row that shows one also names the tool in text.
type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function Ico({ size = 14, children, className, ...rest }: IconProps & { children: ReactNode }) {
  return (
    <svg
      className={`chat-ico ${className ?? ''}`}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      {...rest}
    >
      {children}
    </svg>
  );
}

const PATHS: Record<string, ReactNode> = {
  file: (
    <>
      <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
      <path d="M14 3v6h6" />
    </>
  ),
  edit: (
    <>
      <path d="M4 20h4L19 9l-4-4L4 16z" />
      <path d="M13.5 6.5l4 4" />
    </>
  ),
  play: <path d="M6 4l14 8-14 8z" />,
  shield: (
    <>
      <path d="M12 3l7 3v5c0 5-3.5 8.5-7 10-3.5-1.5-7-5-7-10V6l7-3z" />
      <path d="M9 12l2 2 4-4" />
    </>
  ),
  terminal: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M7 9l3 3-3 3M12 15h5" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="6.5" />
      <path d="M20 20l-4.2-4.2" />
    </>
  ),
  blueprint: (
    <>
      <circle cx="6" cy="6" r="2.5" />
      <circle cx="18" cy="6" r="2.5" />
      <circle cx="12" cy="18" r="2.5" />
      <path d="M7.5 8l3.5 7.5M16.5 8L13 15.5" />
    </>
  ),
  tool: (
    <path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17v3h3l5.3-5.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.5-.5-.5-2.5z" />
  ),
  chevron: <path d="M9 6l6 6-6 6" />,
  stop: <rect x="7" y="7" width="10" height="10" rx="1.5" />,
  check: <path d="M5 12l5 5 9-10" />,
  x: <path d="M18 6L6 18M6 6l12 12" />,
};

export type ChatIconName = keyof typeof PATHS;

export function ChatIcon({ name, ...rest }: IconProps & { name: string }) {
  return <Ico {...rest}>{PATHS[name] ?? PATHS.tool}</Ico>;
}
