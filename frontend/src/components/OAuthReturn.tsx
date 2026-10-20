import { useEffect, useRef, useState } from "react";
import { api, OAUTH_LABELS, oauthStartUrl, type OAuthProvider } from "../api";
import { hashParams, navigate, replaceHash } from "../router";
import type { User } from "../types";
import { Logo } from "./ui";

/** Retour de Google / GitHub : #/auth/<fournisseur>?code=<code à usage unique>. Le code est
 * échangé une seule fois contre la session, puis retiré de l'URL. */
export default function OAuthReturn({
  provider,
  onAuth,
}: {
  provider: OAuthProvider;
  onAuth: (token: string, user: User) => void;
}) {
  const [code] = useState(() => hashParams().get("code"));
  const [error, setError] = useState<string | null>(code ? null : "Lien de connexion incomplet.");
  // Le code ne sert qu'une fois : le mode strict de React rejoue les effets en développement
  const exchanged = useRef(false);

  useEffect(() => {
    if (!code || exchanged.current) return;
    exchanged.current = true;
    replaceHash(`/auth/${provider}`);
    api
      .exchange(code)
      .then((res) => {
        onAuth(res.token, res.user);
        navigate("/");
      })
      .catch((err: Error) => setError(err.message));
  }, [code, provider, onAuth]);

  const label = OAUTH_LABELS[provider];
  return (
    <main className="oauth-return" aria-busy={!error}>
      <Logo />
      {error ? (
        <div className="oauth-return-box">
          <div className="alert" role="alert">
            Connexion avec {label} impossible : {error}
          </div>
          <a className="btn btn-dark btn-lg btn-block" href={oauthStartUrl(provider)}>
            Réessayer
          </a>
          <a className="btn btn-ghost btn-block" href="#/connexion">
            Retour à la connexion
          </a>
        </div>
      ) : (
        <p className="oauth-return-wait" role="status">
          <span className="skeleton oauth-return-bar" aria-hidden="true" />
          Connexion avec {label}…
        </p>
      )}
    </main>
  );
}
