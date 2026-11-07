import type { LucideIcon } from "lucide-react";

interface Props {
  icon: LucideIcon;
  title: string;
  description: string;
  milestone: string;
}

/** Stand-in for a workspace or view that a later milestone will build. */
export function Placeholder({ icon: Icon, title, description, milestone }: Props) {
  return (
    <div className="placeholder">
      <Icon size={40} strokeWidth={1.25} aria-hidden />
      <h2>{title}</h2>
      <p>{description}</p>
      <span className="chip">{milestone}</span>
    </div>
  );
}
