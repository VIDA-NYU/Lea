import type { SVGProps } from 'react';

// Inline 24-unit stroke icons matching the mockup artboards (1.7 stroke, round
// caps). Decorative: every button that uses one also carries an aria-label.
type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function Ico({ size = 16, children, className, ...rest }: IconProps) {
  return (
    <svg
      className={`cv-ico ${className ?? ''}`}
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

export const IconRail = (p: IconProps) => (
  <Ico {...p}>
    <rect x="3" y="3" width="18" height="18" rx="3" />
    <path d="M9 3v18" />
    <path d="M12 9l2 3-2 3" />
  </Ico>
);
export const IconPlay = (p: IconProps) => (
  <Ico {...p}>
    <path d="M6 4l14 8-14 8z" />
  </Ico>
);
export const IconShield = (p: IconProps) => (
  <Ico {...p}>
    <path d="M12 3l7 3v5c0 5-3.5 8.5-7 10-3.5-1.5-7-5-7-10V6l7-3z" />
    <path d="M9 12l2 2 4-4" />
  </Ico>
);
export const IconMore = (p: IconProps) => (
  <Ico {...p}>
    <circle cx="5" cy="12" r="1.2" />
    <circle cx="12" cy="12" r="1.2" />
    <circle cx="19" cy="12" r="1.2" />
  </Ico>
);
export const IconClose = (p: IconProps) => (
  <Ico {...p}>
    <path d="M18 6L6 18M6 6l12 12" />
  </Ico>
);
export const IconChevronLeft = (p: IconProps) => (
  <Ico {...p}>
    <path d="M15 6l-6 6 6 6" />
  </Ico>
);
export const IconChevronRight = (p: IconProps) => (
  <Ico {...p}>
    <path d="M9 6l6 6-6 6" />
  </Ico>
);
export const IconChevronUp = (p: IconProps) => (
  <Ico {...p}>
    <path d="M6 15l6-6 6 6" />
  </Ico>
);
export const IconChevronDown = (p: IconProps) => (
  <Ico {...p}>
    <path d="M6 9l6 6 6-6" />
  </Ico>
);
export const IconChat = (p: IconProps) => (
  <Ico {...p}>
    <path d="M21 12a8 8 0 0 1-8 8H8l-5 3 1-5a8 8 0 1 1 17-6z" />
  </Ico>
);
export const IconFolder = (p: IconProps) => (
  <Ico {...p}>
    <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
  </Ico>
);
export const IconClipboard = (p: IconProps) => (
  <Ico {...p}>
    <rect x="8" y="3" width="8" height="4" rx="1" />
    <path d="M16 5h2a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2" />
  </Ico>
);
export const IconFile = (p: IconProps) => (
  <Ico {...p}>
    <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
    <path d="M14 3v6h6" />
  </Ico>
);
export const IconAlert = (p: IconProps) => (
  <Ico {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 8v4M12 16h.01" />
  </Ico>
);
export const IconCheck = (p: IconProps) => (
  <Ico {...p}>
    <path d="M20 6L9 17l-5-5" />
  </Ico>
);
export const IconFocus = (p: IconProps) => (
  <Ico {...p}>
    <path d="M4 9V5a1 1 0 0 1 1-1h4M20 9V5a1 1 0 0 0-1-1h-4M4 15v4a1 1 0 0 0 1 1h4M20 15v4a1 1 0 0 1-1 1h-4" />
  </Ico>
);
