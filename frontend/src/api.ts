import type { Activity, Board, BoardSummary, Card, Column, Member, User } from "./types";

export const API_BASE: string = import.meta.env.VITE_API_BASE || "http://localhost:8040";
const TOKEN_KEY = "tandem.token";

export const OAUTH_PROVIDERS = ["google", "github"] as const;
export type OAuthProvider = (typeof OAUTH_PROVIDERS)[number];
export const OAUTH_LABELS: Record<OAuthProvider, string> = { google: "Google", github: "GitHub" };
export const isOAuthProvider = (value: string | undefined): value is OAuthProvider =>
  (OAUTH_PROVIDERS as readonly string[]).includes(value ?? "");
/** Départ du parcours OAuth : simple lien, le serveur redirige vers le fournisseur. */
export const oauthStartUrl = (provider: OAuthProvider) => `${API_BASE}/api/auth/${provider}/start`;

export const session = {
  get: (): string | null => {
    try {
      return localStorage.getItem(TOKEN_KEY);
    } catch {
      return null;
    }
  },
  set: (token: string) => {
    try {
      localStorage.setItem(TOKEN_KEY, token);
    } catch {
      /* navigation privée : la session ne survivra pas au rechargement */
    }
  },
  clear: () => {
    try {
      localStorage.removeItem(TOKEN_KEY);
    } catch {
      /* rien à nettoyer */
    }
  },
};

export class ApiError extends Error {
  status: number;
  data: unknown;
  constructor(message: string, status: number, data: unknown) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

/** Notifié quand la session expire, pour renvoyer vers l'écran de connexion. */
let onUnauthorized: () => void = () => {};
export const setUnauthorizedHandler = (fn: () => void) => {
  onUnauthorized = fn;
};

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = session.get();
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...options.headers,
      },
    });
  } catch {
    throw new ApiError("Serveur injoignable. Vérifie ta connexion.", 0, null);
  }
  if (res.status === 401 && token) {
    session.clear();
    onUnauthorized();
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = body.detail;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? humanizeValidation(detail[0])
          : detail?.message || `Erreur ${res.status}`;
    throw new ApiError(message, res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

function humanizeValidation(err: { loc?: string[]; msg?: string } | undefined): string {
  const field = err?.loc?.at(-1);
  const labels: Record<string, string> = { email: "E-mail", password: "Mot de passe", name: "Nom", title: "Titre" };
  return field && labels[field] ? `${labels[field]} : ${err?.msg ?? "invalide"}` : err?.msg ?? "Saisie invalide";
}

const json = (method: string, data?: unknown): RequestInit => ({
  method,
  body: data === undefined ? undefined : JSON.stringify(data),
});

export const api = {
  register: (name: string, email: string, password: string) =>
    request<{ token: string; user: User }>("/api/auth/register", json("POST", { name, email, password })),
  login: (email: string, password: string) =>
    request<{ token: string; user: User }>("/api/auth/login", json("POST", { email, password })),
  me: () => request<User>("/api/me"),
  providers: () => request<Record<OAuthProvider, boolean>>("/api/auth/providers"),
  /** Code à usage unique reçu au retour de Google / GitHub → session. */
  exchange: (code: string) => request<{ token: string; user: User }>("/api/auth/exchange", json("POST", { code })),

  listBoards: () => request<BoardSummary[]>("/api/boards"),
  createBoard: (name: string) => request<{ id: number; name: string }>("/api/boards", json("POST", { name })),
  getBoard: (id: number) => request<Board>(`/api/boards/${id}`),
  renameBoard: (id: number, name: string) => request<{ name: string }>(`/api/boards/${id}`, json("PATCH", { name })),
  deleteBoard: (id: number) => request<void>(`/api/boards/${id}`, { method: "DELETE" }),
  activity: (id: number) => request<Activity[]>(`/api/boards/${id}/activity`),

  addMember: (boardId: number, email: string) =>
    request<Member>(`/api/boards/${boardId}/members`, json("POST", { email })),
  removeMember: (boardId: number, userId: number) =>
    request<void>(`/api/boards/${boardId}/members/${userId}`, { method: "DELETE" }),

  createColumn: (boardId: number, name: string) =>
    request<Column>(`/api/boards/${boardId}/columns`, json("POST", { name })),
  renameColumn: (id: number, name: string) => request<Omit<Column, "cards">>(`/api/columns/${id}`, json("PATCH", { name })),
  moveColumn: (id: number, index: number) => request<Omit<Column, "cards">>(`/api/columns/${id}/move`, json("POST", { index })),
  deleteColumn: (id: number) => request<void>(`/api/columns/${id}`, { method: "DELETE" }),

  createCard: (columnId: number, title: string) =>
    request<Card>(`/api/columns/${columnId}/cards`, json("POST", { title })),
  updateCard: (id: number, data: { version: number; title?: string; description?: string; assignee_id?: number | null }) =>
    request<Card>(`/api/cards/${id}`, json("PATCH", data)),
  moveCard: (id: number, columnId: number, index: number) =>
    request<Card>(`/api/cards/${id}/move`, json("POST", { column_id: columnId, index })),
  deleteCard: (id: number) => request<void>(`/api/cards/${id}`, { method: "DELETE" }),
};

/** Le jeton n'est pas dans l'URL (elle finirait dans les journaux d'accès) : il est envoyé en
 * premier message, `{ type: "auth", token }`, dès l'ouverture de la connexion. */
export const socketUrl = (boardId: number) => `${API_BASE.replace(/^http/, "ws")}/ws/boards/${boardId}`;
