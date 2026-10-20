import { useEffect, useState } from "react";

// Routage minimal par hash : pas besoin d'une dépendance pour 7 écrans.
export function useHashRoute(): string[] {
  const read = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const [parts, setParts] = useState(read);
  useEffect(() => {
    const onChange = () => setParts(read());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return parts;
}

export const navigate = (path: string) => {
  window.location.hash = path;
};
