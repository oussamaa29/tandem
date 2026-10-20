# Tandem — Kanban collaboratif en temps réel

[![CI](https://github.com/oussamaa29/tandem/actions/workflows/ci.yml/badge.svg)](https://github.com/oussamaa29/tandem/actions/workflows/ci.yml)

Tableaux Kanban partagés : quand un membre déplace une carte, la renomme ou l'assigne, tous les autres le voient **instantanément**, sans recharger. Présence en direct, indicateur « Sarah modifie… », historique d'activité, et gestion explicite des conflits d'édition.

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
| Auth | bcrypt (sur un condensé SHA-256, pour les mots de passe de plus de 72 octets) + JWT (en en-tête pour REST ; pour le WebSocket, en premier message après l'ouverture, jamais dans l'URL qui finirait dans les journaux d'accès) |
| Tests | pytest, dont des tests multi-utilisateurs sur WebSocket, des courses entre requêtes simultanées et la vérification des migrations (48 tests), exécutés en CI sur SQLite **et** PostgreSQL 16 |

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
alembic revision --autogenerate -m "ajoute les étiquettes" --rev-id 0002
alembic upgrade head
```

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
```

Les tests créent le schéma avec les migrations Alembic (pas `create_all`) et vérifient qu'il correspond exactement aux modèles. La CI exécute la suite sur SQLite et sur un service PostgreSQL 16.

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
- **Mise à l'échelle** : le hub de diffusion est en mémoire (un seul processus). Pour plusieurs instances, `hub.publish` passerait par un pub/sub (Redis, ou `LISTEN/NOTIFY` de Postgres) sans changer le reste du code.

## Accessibilité & UX

- Glisser-déposer au clavier (Espace pour saisir, flèches, Espace pour déposer, Échap pour annuler) avec annonces pour lecteurs d'écran
- Dialogues natifs `<dialog>` (piège à focus et Échap fournis par le navigateur)
- États de chargement en squelettes, états vides, erreurs explicites, pastille d'état de connexion
- Surbrillance des cartes modifiées par les autres, avatars de présence, mode sombre automatique, mise en page mobile

## Structure

```
tandem/
├── backend/
│   ├── main.py        # Routes REST + endpoint WebSocket
│   ├── hub.py         # Rooms, présence, diffusion
│   ├── positions.py   # Indexation fractionnaire + rééquilibrage
│   ├── models.py      # User, Board, Membership, BoardColumn, Card, Activity
│   ├── database.py    # Moteur SQLite / Postgres, pool, application des migrations
│   ├── migrations/    # Migrations Alembic (schéma versionné)
│   ├── auth.py        # bcrypt + JWT
│   └── tests/         # positions, droits, cartes, verrou optimiste, temps réel, migrations
└── frontend/src/
    ├── useBoard.ts    # Connexion WebSocket, reconnexion, tampon, détection de trous
    ├── boardState.ts  # Reducer : événements serveur + actions optimistes (idempotents)
    └── components/    # Tableau, colonnes, cartes, modale d'édition, membres, activité
```
