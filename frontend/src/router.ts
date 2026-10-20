import { useEffect, useState } from "react";

const hashPath = () => window.location.hash.replace(/^#\/?/, "").split("?")[0];

// Routage minimal par hash : pas besoin d'une dépendance pour 7 écrans.
export function useHashRoute(): string[] {
  const read = () => hashPath().split("/").filter(Boolean);
  const [parts, setParts] = useState(read);
  useEffect(() => {
    const onChange = () => setParts(read());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return parts;
}

/** Paramètres après le « ? » du hash (#/auth/google?code=…, #/connexion?erreur=…). */
export const hashParams = () => new URLSearchParams(window.location.hash.split("?")[1] ?? "");

export const navigate = (path: string) => {
  window.location.hash = path;
};

/** Remplace le hash sans ajouter d'entrée à l'historique (ex. retirer un code déjà utilisé). */
export const replaceHash = (path: string) => {
  window.history.replaceState(null, "", `#${path}`);
};
