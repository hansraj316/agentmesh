const COLORS: Record<string, string> = {
  active: "var(--blue)",
};

export function Avatar({ name, active = false }: { name: string; active?: boolean }) {
  const initial = (name.trim()[0] ?? "?").toUpperCase();
  return (
    <span
      className={"avatar" + (active ? " avatar--active" : "")}
      style={active ? { background: COLORS.active } : undefined}
      title={name}
      aria-hidden="true"
    >
      {initial}
    </span>
  );
}

export function AvatarPair({ from, to, active }: { from: string; to: string; active: boolean }) {
  return (
    <span className="avatar-pair">
      <Avatar name={from} active={active} />
      <Avatar name={to || "?"} />
    </span>
  );
}
