interface Props {
  width?: string;
  height?: string;
  variant?: 'text' | 'rect';
}

export default function SkeletonBlock({
  width = '100%',
  height,
  variant = 'rect',
}: Props) {
  const h = height ?? (variant === 'text' ? '0.9rem' : '2rem');
  return (
    <div
      className="sb-shimmer"
      style={{ width, height: h, marginBottom: variant === 'text' ? '0.4rem' : 0 }}
    />
  );
}
