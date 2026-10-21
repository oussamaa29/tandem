import { useCallback, useEffect, useState } from "react";
import { api, isOAuthProvider, session, setUnauthorizedHandler } from "./api";
import { navigate, useHashRoute } from "./router";
import type { User } from "./types";
import AuthScreen from "./components/AuthScreen";
import OAuthReturn from "./components/OAuthReturn";
import BoardsHome from "./components/BoardsHome";
import BoardView from "./components/BoardView";
import { Icon, Logo } from "./components/ui";
import { useToast } from "./toast";

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  // Ouverture sur le retour OAuth : la session à venir remplace l'ancienne, inutile de la vérifier
  const [checking, setChecking] = useState(() => Boolean(session.get()) && !window.location.hash.startsWith("#/auth/"));
  const route = useHashRoute();
  const toast = useToast();

  const onAuth = useCallback((token: string, u: User) => {
    session.set(token);
    setUser(u);
    setChecking(false);
  }, []);

  const logout = useCallback(() => {
    session.clear();
    setUser(null);
    navigate("/");
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setUser(null);
      toast("Ta session a expiré, reconnecte-toi.", "error");
    });
  }, [toast]);

  useEffect(() => {
    if (!session.get() || window.location.hash.startsWith("#/auth/")) return;
    api
      .me()
      .then(setUser)
      .catch(() => session.clear())
      .finally(() => setChecking(false));
  }, []);

  const [section, id] = route;
  // Retour de Google / GitHub : prioritaire, même si une ancienne session existe
  if (section === "auth" && isOAuthProvider(id)) {
    return <OAuthReturn provider={id} onAuth={onAuth} />;
  }

  if (checking) return null;
  if (!user) return <AuthScreen onAuth={onAuth} />;

  const boardId = section === "b" && id ? Number(id) : null;

  return (
    <div className="app">
      {boardId && Number.isInteger(boardId) ? (
        <BoardView key={boardId} boardId={boardId} me={user} onLogout={logout} />
      ) : (
        <>
          <header className="topbar">
            <a href="#/" aria-label="Accueil">
              <Logo />
            </a>
            <div className="spacer" />
            <span className="whoami hide-mobile" title={user.email}>{user.name}</span>
            <button className="btn btn-ghost" onClick={logout}>
              <Icon name="logout" size={15} />
              <span className="hide-mobile">Déconnexion</span>
            </button>
          </header>
          <BoardsHome me={user} />
        </>
      )}
    </div>
  );
}
