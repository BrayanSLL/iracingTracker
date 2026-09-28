# Contribuer à iRacing Telemetry Logger

Merci de ton intérêt ! Toute aide est la bienvenue : bugs, idées, code, documentation, traductions.

## Avant de coder

- **Une petite correction** (faute, bug évident) : ouvre directement une pull request.
- **Une nouvelle fonctionnalité** : ouvre d'abord une issue « Idée de fonctionnalité » pour en discuter.
  Ça évite de passer du temps sur quelque chose qui ne rentre pas dans le projet.
- Les idées déjà prévues sont listées dans la [feuille de route](README.md#feuille-de-route).

## Installation pour développer

Python 3.9 ou plus récent.

```bash
git clone https://github.com/BrayanSLL/iracingTracker.git
cd iracingTracker
python -m venv venv
# Windows : venv\Scripts\activate    —    macOS / Linux : source venv/bin/activate
pip install -r requirements.txt
```

Hors Windows, `pyirsdk` et `pywebview` ne servent à rien, mais tu peux quand même tout développer :

```bash
pip install flask
python main.py --demo --demo-speed 10 --browser
```

## Le mode démo : pas besoin d'iRacing

`python main.py --demo` remplace iRacing par une voiture simulée (`demo.py`) sur un circuit de 4 km. Elle
produit toutes les données réelles : vitesse, pédales, rapports, régime, G, ABS, cap, carburant, temps au
tour avec le même léger retard que le vrai iRacing. La démo tire aussi au sort des **défauts de pilotage**
(freinage timide, hésitation à l'accélération, rapport trop long, passages de rapport trop tôt), que
l'analyse doit retrouver.

Si tu ajoutes une donnée lue dans iRacing, ajoute-la aussi dans `FakeIRSDK.__getitem__` (`demo.py`).

## Tests

```bash
python -m unittest discover tests
```

Les tests tournent sans iRacing (voiture simulée, base temporaire) et sont lancés automatiquement sur GitHub
à chaque pull request. **Une pull request doit laisser tous les tests au vert**, et une nouvelle fonctionnalité
doit arriver avec ses tests.

## Organisation du code

| Fichier | Rôle |
|---|---|
| `main.py` | Point d'entrée, API Flask, fenêtres (pywebview) |
| `telemetry.py` | Lecture d'iRacing à 60 Hz, détection des tours, delta en direct, mode entraînement, annonces |
| `analysis.py` | Nettoyage et alignement des traces, secteurs, virages, carte, statistiques |
| `technique.py` | Analyse du pilotage façon ingénieur (seuils regroupés en haut du fichier) |
| `debrief.py` | Débrief de session, comparaison de sessions, progression, habitudes, résumé radio |
| `objectives.py` | Objectifs, XP et niveaux |
| `db.py` | Base SQLite : schéma, migrations, requêtes |
| `voice.py` | Synthèse vocale Windows |
| `demo.py` | Faux iRacing pour la démo et les tests |
| `templates/`, `static/` | Interface (HTML, CSS, JavaScript sans framework, graphiques uPlot) |

## Conventions

- **Langue :** l'interface, les messages, les commentaires et la documentation sont en **français**. Le
  tutoiement est utilisé partout dans l'interface.
- **Style :** suis le style du code autour (PEP 8 côté Python, lignes d'environ 120 caractères).
- **Base de données :** ne modifie jamais une table existante à la main. Ajoute les nouvelles colonnes dans
  `MIGRATIONS` (`db.py`) pour que les bases des utilisateurs soient mises à niveau automatiquement.
- **Pas de service payant ni d'accès internet obligatoire :** tout doit fonctionner hors ligne, sur le PC de
  l'utilisateur. Les dépendances JavaScript sont incluses dans `static/vendor/`, pas chargées depuis un CDN.
- **Seuils d'analyse :** regroupe-les en constantes nommées en haut du fichier, avec un commentaire qui explique
  leur sens.
- **Données personnelles :** ne versionne jamais de fichier du dossier `data/` (sessions des utilisateurs).

## Proposer une pull request

1. Crée une branche à partir de `main`.
2. Fais des commits clairs, en français de préférence.
3. Vérifie que `python -m unittest discover tests` passe.
4. Dans la description, explique **quoi** et **pourquoi**. Pour un changement d'interface, ajoute une capture
   d'écran.

## Signaler un bug

Utilise le modèle « Bug » des issues et donne si possible :
- la voiture, le circuit et le type de session (essais, qualif, course)
- ce que tu attendais et ce qui s'est passé
- les messages de la console (la fenêtre où tu as lancé `python main.py`)

N'envoie pas ta base `data/sessions.db` publiquement : elle contient tout ton historique de pilotage.
