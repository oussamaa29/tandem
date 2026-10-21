import { useEffect, useRef, useState } from "react";
import { api, OAUTH_LABELS, session, startOAuth, storedSessionUser, takeOAuthNonce, type OAuthProvider } from "../api";
import { hashParams, navigate, replaceHash } from "../router";
import type { User } from "../types";
import { Logo } from "./ui";

type Session = { token: string; user: User };
type State =
  | { kind: "loading" }
  | { kind: "error"; message: string; current: Session | null }
  | { kind: "confirm"; current: Session; next: Session };

/** Retour de Google / GitHub : #/auth/<fournisseur>?code=<code à usage unique>. Le code est
 * échangé une seule fois, avec le nonce gardé par l'onglet qui a lancé la connexion, puis retiré
 * de l'URL. Si une session est déjà ouverte pour un AUTRE compte, on demande avant de la remplacer. */
export default function OAuthReturn({
  provider,
  onAuth,
}: {
  provider: OAuthProvider;
  onAuth: (token: string, user: User) => void;
}) {
  const [state, setState] = useState<State>({ kind: "loading" });
  // Le code ne sert qu'une fois : le mode strict de React rejoue les effets en développement
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const code = hashParams().get("code");
    const nonce = takeOAuthNonce();
    replaceHash(`/auth/${provider}`);

    (async () => {
      const currentToken = session.get();
      const currentUser = await storedSessionUser();
      const current = currentToken && currentUser ? { token: currentToken, user: currentUser } : null;
      if (!code || !nonce) {
        // Pas de nonce : ce lien n'a pas été ouvert par l'onglet qui a lancé la connexion (lien
        // reçu d'un tiers, autre onglet…) ; il ne pourrait de toute façon pas être échangé
        setState({ kind: "error", current, message: "ce lien ne vient pas de cet onglet. Relance la connexion." });
        return;
      }
      try {
        const next = await api.exchange(code, nonce);
        if (!current || current.user.id === next.user.id) {
          onAuth(next.token, next.user);
          navigate("/");
        } else {
          setState({ kind: "confirm", current, next });
        }
      } catch (err) {
        setState({ kind: "error", current, message: (err as Error).message });
      }
    })();
  }, [provider, onAuth]);

  const label = OAUTH_LABELS[provider];
  const choose = (s: Session) => {
    onAuth(s.token, s.user);
    navigate("/");
  };
  const retry = () => {
    if (!startOAuth(provider) && state.kind === "error") {
      setState({ ...state, message: "ton navigateur bloque le stockage nécessaire à la connexion." });
    }
  };

  return (
    <main className="oauth-return" aria-busy={state.kind === "loading"}>
      <Logo />
      {state.kind === "loading" && (
        <p className="oauth-return-wait" role="status">
          <span className="skeleton oauth-return-bar" aria-hidden="true" />
          Connexion avec {label}…
        </p>
      )}
      {state.kind === "confirm" && (
        <div className="oauth-return-box">
          <div className="alert alert-info" role="alert">
            Tu es connecté en tant que <strong>{state.current.user.name}</strong> ({state.current.user.email}).
            Continuer en tant que <strong>{state.next.user.name}</strong> ({state.next.user.email}) ?
          </div>
          <button className="btn btn-dark btn-lg btn-block" onClick={() => choose(state.next)}>
            Continuer en tant que {state.next.user.name}
          </button>
          <button className="btn btn-ghost btn-block" onClick={() => choose(state.current)}>
            Rester connecté en tant que {state.current.user.name}
          </button>
        </div>
      )}
      {state.kind === "error" && (
        <div className="oauth-return-box">
          <div className="alert" role="alert">
            Connexion avec {label} impossible : {state.message}
          </div>
          <button className="btn btn-dark btn-lg btn-block" onClick={retry}>
            Réessayer
          </button>
          {state.current ? (
            // Une session encore valide existe : on y revient plutôt qu'à l'écran de connexion
            <button className="btn btn-ghost btn-block" onClick={() => state.current && choose(state.current)}>
              Revenir à mes tableaux ({state.current.user.name})
            </button>
          ) : (
            <a className="btn btn-ghost btn-block" href="#/connexion">
              Retour à la connexion
            </a>
          )}
        </div>
      )}
    </main>
  );
}
