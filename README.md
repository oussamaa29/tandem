# Tandem — Kanban collaboratif en temps réel

[![CI](https://github.com/oussamaa29/tandem/actions/workflows/ci.yml/badge.svg)](https://github.com/oussamaa29/tandem/actions/workflows/ci.yml)

Tableaux Kanban partagés : quand un membre déplace une carte, la renomme ou l'assigne, tous les autres le voient **instantanément**, sans recharger. Présence en direct, indicateur « Sarah modifie… », historique d'activité, et gestion explicite des conflits d'édition.

![Deux utilisateurs sur le même tableau : Karim déplace des cartes, Sarah les voit arriver ; Sarah ouvre une carte, Karim voit « Sarah modifie… »](docs/demo.gif)

**En 30 secondes**
- **Temps réel fiable** : révision par tableau, détection des événements manqués et resynchronisation, reconnexion automatique.
- **Concurrence maîtrisée** : verrou optimiste (409 au lieu d'un écrasement silencieux), indexation fractionnaire, verrous Postgres ordonnés contre les interblocages.
- **Passage à l'échelle** : plusieurs instances reliées par Redis (pub/sub + présence partagée).
- **Sécurité** : JWT jamais dans une URL, OAuth Google / GitHub avec PKCE et protection contre la prise de contrôle de compte.
- **Testé** : 104 tests backend (dont des courses entre requêtes et des scénarios multi-instances), 17 tests frontend, CI sur SQLite, PostgreSQL 16 et Redis 7.

## Pourquoi ce projet

Un CRUD affiche des données ; une app collaborative doit les garder **cohérentes entre plusieurs personnes qui agissent en même temps**. Ce projet traite les vrais problèmes de ce genre d'app :

| Problème | Solution retenue |
|---|---|
| Diffuser chaque modification à tous les membres | WebSocket par tableau (« room ») ; les écritures passent par l'API REST, puis l'événement est diffusé |
| Ne jamais rater une modification (coupure réseau, onglet en veille) | Chaque tableau a une **révision** incrémentée à chaque écriture. Un client qui voit un trou dans la séquence attend brièvement l'événement manquant (deux diffusions simultanées peuvent arriver dans le désordre), puis se resynchronise. Reconnexion automatique avec backoff exponentiel + gigue |
| Pas de fenêtre aveugle au chargement | Le client rejoint la room WebSocket **avant** de charger le tableau, met en tampon les événements reçus pendant le chargement, puis les rejoue |
| Deux personnes éditent la même carte | **Verrou optimiste** (`version`, vérifiée dans l'`UPDATE … WHERE version = :v` lui-même, donc correct même pour deux requêtes simultanées) : l'enregistrement périmé est refusé (409) et l'utilisateur choisit « voir sa version » ou « garder la mienne ». Jamais d'écrasement silencieux |
| Réordonner sans réécrire toute la colonne | **Indexation fractionnaire** : une carte insérée prend la moyenne des positions de ses voisines (1 ligne modifiée). Rééquilibrage automatique quand l'écart devient trop faible. Deux positions égales (déplacements simultanés) sont départagées par id, de façon identique côté serveur et client |
| Interface réactive malgré la latence | **UI optimiste** : la carte bouge tout de suite. En cas d'échec, l'état est restauré depuis le serveur |
| Membre retiré pendant qu'il est connecté | Sa connexion est fermée côté serveur (code 4403) et ses cartes assignées sont libérées |

## Stack

| Couche | Techno |
|---|---|
| Frontend | React 19 + TypeScript (Vite), dnd-kit (glisser-déposer accessible au clavier), CSS maison avec design tokens et mode sombre |
| Backend | FastAPI, WebSockets, SQLAlchemy 2, migrations Alembic |
| Base de données | SQLite par défaut (zéro installation), PostgreSQL (psycopg 3) via `DATABASE_URL` |
| Auth | bcrypt (sur un condensé SHA-256, pour les mots de passe de plus de 72 octets) + JWT (en en-tête pour REST ; pour le WebSocket, en premier message après l'ouverture, jamais dans l'URL qui finirait dans les journaux d'accès) ; « Continuer avec Google / GitHub » en option (OAuth 2.0 + PKCE, OpenID Connect pour Google) |
| Temps réel multi-instances | Redis (pub/sub + présence partagée), optionnel via `REDIS_URL` |
| Tests | pytest, dont des tests multi-utilisateurs sur WebSocket, des courses entre requêtes simultanées, plusieurs instances reliées par Redis, la connexion Google / GitHub (simulée, sans réseau) et la vérification des migrations (104 tests), exécutés en CI sur SQLite, PostgreSQL 16 **et** Redis 7 ; Vitest pour le reducer temps réel du frontend (17 tests) |

Aucun service payant.

## Lancer le projet

### Backend

```bash
cd backend
python -m venv venv
venv/Scripts/activate        # Windows (source venv/bin/activate sur macOS/Linux)
pip install -r requirements.txt
cp .env.example .env         # puis remplir JWT_SECRET
uvicorn main:app --reload --port 8040
```

Sans `JWT_SECRET`, un secret de développement est utilisé avec un avertissement au démarrage ; avec `APP_ENV=production`, le serveur refuse de démarrer.

#### Base de données et migrations

Le schéma est géré par **Alembic** (`backend/migrations/`).

- **SQLite (par défaut)** : rien à faire, les migrations sont appliquées automatiquement au démarrage du serveur (une seule instance en local, aucun risque). Une base `tandem.db` créée avant Alembic est reconnue et marquée comme étant à jour.
- **PostgreSQL** : définir `DATABASE_URL=postgresql://user:pass@hôte:5432/tandem` (converti automatiquement vers le pilote psycopg 3), puis appliquer le schéma **avant** de lancer le serveur, comme étape explicite du déploiement :

  ```bash
  alembic upgrade head
  ```

  Le serveur ne migre jamais Postgres lui-même (plusieurs instances le feraient en même temps). Taille du pool réglable avec `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` (5 / 10 par défaut).

Modifier le schéma : changer `models.py`, puis générer et relire la migration (le test `test_migrations.py` échoue tant que modèles et migrations divergent) :

```bash
alembic revision --autogenerate -m "ajoute les étiquettes" --rev-id 0003
alembic upgrade head
```

#### Connexion avec Google / GitHub (facultatif)

Les boutons « Continuer avec Google » et « Continuer avec GitHub » n'apparaissent que si le fournisseur est configuré (identifiant **et** secret dans `backend/.env`). Les deux sont gratuits.

**Google** ([Google Cloud Console](https://console.cloud.google.com/)) :

1. Créer (ou choisir) un projet, puis *API et services → Écran de consentement OAuth* : type « Externe », laisser l'application en mode **Test** et ajouter son adresse dans les **utilisateurs test** (seuls eux pourront se connecter tant que l'application n'est pas publiée).
2. *Identifiants → Créer des identifiants → ID client OAuth*, type **Application Web**.
3. *URI de redirection autorisés* : `http://localhost:8040/api/auth/google/callback`.
4. Reporter l'ID client et le secret dans `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.

**GitHub** (*Settings → Developer settings → OAuth Apps → New OAuth App*) :

1. *Homepage URL* : `http://localhost:5176` ; *Authorization callback URL* : `http://localhost:8040/api/auth/github/callback`.
2. Générer un secret client, puis renseigner `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET`.

En production, définir aussi `PUBLIC_API_URL` (adresse publique de l'API, qui sert à construire l'URL de retour déclarée chez le fournisseur) et `FRONTEND_URL` (où renvoyer l'utilisateur), et déclarer les URL de retour correspondantes.

Les protections du parcours (PKCE, `state`, vérification de l'id_token, rattachement de comptes sans prise de contrôle, code à usage unique lié à l'onglet contre le *login CSRF*) sont détaillées dans [docs/SECURITE-OAUTH.md](docs/SECURITE-OAUTH.md).

### Frontend

```bash
cd frontend
npm install
cp .env.example .env
npm run dev
```

Ouvre http://localhost:5176 dans **deux navigateurs différents** (ou une fenêtre normale + une privée), crée deux comptes, invite le second depuis « Partager »… et déplace des cartes.

### Tests

```bash
cd backend
pytest                                   # SQLite temporaire, aucune installation
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/tandem_test pytest   # sur Postgres (base vidée !)
TEST_REDIS_URL=redis://localhost:6379/0 REDIS_URL=redis://localhost:6379/0 pytest   # hub sur un vrai Redis
```

```bash
cd frontend
npm test                                 # Vitest : reducer du tableau (événements serveur, actions optimistes), utilitaires
```

Les tests créent le schéma avec les migrations Alembic (pas `create_all`) et vérifient qu'il correspond exactement aux modèles. Les tests multi-instances (`test_hub_redis.py`) utilisent fakeredis en mémoire, ou un vrai Redis avec `TEST_REDIS_URL`. La CI exécute la suite sur SQLite, sur un service PostgreSQL 16, et sur un service Redis 7 (toute la suite en mode distribué).

## Architecture du temps réel

```
 navigateur A                         serveur                          navigateur B
     │  POST /api/cards/12/move  ───▶  valide + écrit en base
     │                                 revision = revision + 1  (SQL atomique)
     │  ◀─── 200 (carte à jour)        commit
     │                                 publication après la réponse ──▶  { type: "card.moved",
     │  ◀──────────── même événement (écho, ignoré car déjà appliqué)      revision: 42, … }
```

- **Écritures en REST, lecture en WebSocket** : chaque modification est validée et transactionnelle, puis diffusée. Le WebSocket ne transporte vers le serveur que l'authentification (`{ type: "auth", token }`, premier message, 5 s maximum) et des signaux éphémères (présence, « je modifie la carte 12 »).
- **Événements idempotents** côté client : appliquer deux fois le même événement, ou recevoir l'écho de sa propre action, ne change rien.
- **Mise à l'échelle** : sans `REDIS_URL`, le hub est en mémoire (un seul processus, idéal en local). Avec `REDIS_URL`, plusieurs instances se partagent le temps réel (voir ci-dessous) ; les routes n'en savent rien, l'interface du hub est la même.

### Plusieurs instances

Avec `REDIS_URL`, plusieurs instances se partagent les événements (pub/sub) et la présence, sans affinité de session. Comportement en cas de client lent, d'instance arrêtée ou de Redis indisponible : [docs/TEMPS-REEL.md](docs/TEMPS-REEL.md).

## Accessibilité & UX

- Glisser-déposer au clavier (Espace pour saisir, flèches, Espace pour déposer, Échap pour annuler) avec annonces pour lecteurs d'écran
- Dialogues natifs `<dialog>` (piège à focus et Échap fournis par le navigateur)
- États de chargement en squelettes, états vides, erreurs explicites, pastille d'état de connexion
- Surbrillance des cartes modifiées par les autres, avatars de présence, mode sombre automatique, mise en page mobile

## Structure

```
tandem/
├── backend/
│   ├── main.py        # Application FastAPI : démarrage, CORS, branchement des routes
│   ├── routers/       # auth, boards (tableaux + membres), columns, cards, realtime (WebSocket)
│   ├── access.py      # Contrôle d'accès + verrou d'écriture du tableau
│   ├── events.py      # Révision, journal d'activité, diffusion après la réponse
│   ├── serializers.py # Modèles -> JSON
│   ├── hub.py         # Rooms, présence, diffusion (en mémoire, ou entre instances via Redis)
│   ├── positions.py   # Indexation fractionnaire + rééquilibrage
│   ├── models.py      # User, Board, Membership, BoardColumn, Card, Activity
│   ├── database.py    # Moteur SQLite / Postgres, pool, application des migrations
│   ├── migrations/    # Migrations Alembic (schéma versionné)
│   ├── auth.py        # bcrypt + JWT
│   ├── oauth.py       # Google / GitHub : state + PKCE, identité, rattachement, code à usage unique
│   └── tests/         # positions, droits, cartes, verrou optimiste, temps réel, migrations
└── frontend/src/
    ├── useBoard.ts    # Connexion WebSocket, reconnexion, tampon, détection de trous
    ├── boardState.ts  # Reducer : événements serveur + actions optimistes (idempotents), testé avec Vitest
    └── components/    # Tableau, colonnes, cartes, modale d'édition, membres, activité
```

## Licence

MIT
