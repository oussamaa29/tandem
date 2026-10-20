import { useEffect, useState, type FormEvent } from "react";
import { api, OAUTH_LABELS, OAUTH_PROVIDERS, oauthStartUrl, type OAuthProvider } from "../api";
import { hashParams, replaceHash } from "../router";
import type { User } from "../types";
import { Logo } from "./ui";

type Mode = "login" | "register";

/** Codes renvoyés par le serveur dans #/connexion?erreur=<code> après un échec OAuth. */
const OAUTH_ERRORS: Record<string, string> = {
  annule: "Connexion annulée.",
  etat: "La demande de connexion a expiré ou vient d'un autre navigateur. Réessaie.",
  fournisseur: "Le service de connexion ne répond pas pour le moment. Réessaie dans un instant.",
  jeton: "La connexion n'a pas pu être vérifiée. Réessaie.",
  email_non_verifie: "Ton adresse e-mail n'est pas vérifiée chez ce fournisseur : vérifie-la, puis réessaie.",
  non_configure: "Cette méthode de connexion n'est pas disponible sur ce serveur.",
};

const oauthErrorFromUrl = (): string | null => {
  const code = hashParams().get("erreur");
  return code ? (OAUTH_ERRORS[code] ?? "Une erreur est survenue pendant la connexion. Réessaie.") : null;
};

/** Marques monochromes (pas de logo multicolore, pour rester dans la charte noir / blanc / vert). */
function ProviderMark({ provider }: { provider: OAuthProvider }) {
  if (provider === "google") {
    return <span className="oauth-mark" aria-hidden="true">G</span>;
  }
  return (
    <svg className="oauth-mark" viewBox="0 0 16 16" width="16" height="16" fill="currentColor" aria-hidden="true">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

export default function AuthScreen({ onAuth }: { onAuth: (token: string, user: User) => void }) {
  const [mode, setMode] = useState<Mode>("login");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(oauthErrorFromUrl);
  const [busy, setBusy] = useState(false);
  const [providers, setProviders] = useState<OAuthProvider[]>([]);

  useEffect(() => {
    // Message affiché une fois : l'URL d'erreur ne doit pas resservir après un rechargement
    if (hashParams().get("erreur")) replaceHash("/connexion");
    api
      .providers()
      .then((enabled) => setProviders(OAUTH_PROVIDERS.filter((p) => enabled[p])))
      .catch(() => setProviders([])); // serveur injoignable : le formulaire affichera l'erreur
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = mode === "login" ? await api.login(email, password) : await api.register(name, email, password);
      onAuth(res.token, res.user);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };

  const switchMode = (m: Mode) => {
    setMode(m);
    setError(null);
  };

  return (
    <div className="auth">
      <aside className="auth-aside">
        <Logo />
        <h2>
          Un tableau.
          <br />
          Plusieurs personnes.
          <br />
          <span className="accent-text">Le même état, partout.</span>
        </h2>
        <ul className="auth-facts">
          <li>Chaque déplacement apparaît chez les autres sans recharger.</li>
          <li>Deux modifications de la même carte ne s'écrasent jamais.</li>
          <li>Une coupure réseau se rattrape toute seule.</li>
        </ul>
      </aside>

      <main className="auth-main">
        <form className="auth-form" onSubmit={submit}>
          <div>
            <h1>{mode === "login" ? "Connexion" : "Créer un compte"}</h1>
            <p className="sub">
              {mode === "login" ? "Retrouve tes tableaux." : "Un nom, un e-mail, un mot de passe."}
            </p>
          </div>

          {providers.length > 0 && (
            <>
              <div className="oauth-buttons">
                {providers.map((p) => (
                  <a key={p} className="btn btn-lg btn-block" href={oauthStartUrl(p)}>
                    <ProviderMark provider={p} />
                    Continuer avec {OAUTH_LABELS[p]}
                  </a>
                ))}
              </div>
              <div className="divider" role="separator">
                <span>ou</span>
              </div>
            </>
          )}

          <div className="segmented" role="group" aria-label="Mode">
            <button type="button" aria-pressed={mode === "login"} onClick={() => switchMode("login")}>
              Connexion
            </button>
            <button type="button" aria-pressed={mode === "register"} onClick={() => switchMode("register")}>
              Inscription
            </button>
          </div>

          {error && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}

          {mode === "register" && (
            <label className="field">
              <span>Nom affiché</span>
              <input className="input" required maxLength={80} autoComplete="name" value={name}
                onChange={(e) => setName(e.target.value)} placeholder="Oussama" />
            </label>
          )}
          <label className="field">
            <span>E-mail</span>
            <input className="input" type="email" required autoComplete="email" value={email}
              onChange={(e) => setEmail(e.target.value)} placeholder="toi@exemple.com" />
          </label>
          <label className="field">
            <span>Mot de passe</span>
            <input className="input" type="password" required minLength={mode === "register" ? 8 : undefined}
              autoComplete={mode === "login" ? "current-password" : "new-password"} value={password}
              onChange={(e) => setPassword(e.target.value)} placeholder={mode === "register" ? "8 caractères minimum" : ""} />
          </label>

          <button className="btn btn-primary btn-lg btn-block" disabled={busy}>
            {busy ? "Un instant…" : mode === "login" ? "Se connecter" : "Créer mon compte"}
          </button>
          <p className="hint">Astuce : ouvre l'app dans deux navigateurs pour voir la synchronisation en direct.</p>
        </form>
      </main>
    </div>
  );
}
