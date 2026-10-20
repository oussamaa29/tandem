const rtf = new Intl.RelativeTimeFormat("fr", { numeric: "auto" });

/** Les dates du serveur sont en UTC sans suffixe : on l'ajoute avant de parser. */
export function parseServerDate(iso: string): Date {
  return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
}

export function timeAgo(iso: string, now = Date.now()): string {
  const seconds = Math.round((parseServerDate(iso).getTime() - now) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 45) return "à l'instant";
  if (abs < 3600) return rtf.format(Math.round(seconds / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(seconds / 3600), "hour");
  if (abs < 86400 * 7) return rtf.format(Math.round(seconds / 86400), "day");
  return parseServerDate(iso).toLocaleDateString("fr-FR", { day: "numeric", month: "short" });
}

export const initials = (name: string) =>
  name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p[0]!.toUpperCase())
    .join("");

export const plural = (n: number, one: string, many: string) => `${n} ${n > 1 ? many : one}`;
