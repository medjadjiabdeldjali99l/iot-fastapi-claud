# Documentation fonctionnelle — IoT Cadence

> Étape 2/3 — Documentation **fonctionnelle**. Décrit *ce que fait* la solution
> du point de vue métier et utilisateur (pas le *comment* technique, cf.
> [DOCUMENTATION_TECHNIQUE.md](DOCUMENTATION_TECHNIQUE.md)).

---

## 1. Fonctionnalités principales & comportement attendu

La solution permet à un **admin d'usine** de surveiller la **cadence de production**
(objets/minute, « OPM ») d'une chaîne, à partir d'une caméra placée en fin de ligne.
Trois modules.

### Module 1 — Contrôle caméra

| Fonctionnalité | Comportement attendu |
|---|---|
| **Statut caméra** | `online` / `offline` / `unknown`, par ping (HTTP/RTSP, ou toujours `online` pour une webcam locale). Vérification réelle à l'ouverture du flux. |
| **Preview live** | Flux vidéo temps réel dans l'interface (WebSocket, JPEG). Ouvrir/fermer libère la ressource. |
| **Ligne de détection (trigger line)** | Ligne verticale déplaçable sur l'image (ratio 0–1 de la largeur). Défaut 80 %. C'est le « portique » de comptage. |
| **Confiance YOLO** | Seuil 0–1 (défaut 0.5). Ignoré en mode OpenCV. |
| **Modèle YOLO** | `yolov8n` / `yolov8l` / `yolov11n` / `yolov11l`. |
| **Plage de cadence de référence (OPM)** | Min/max attendus. Saisie côté UI, **persistée sur la session** au lancement (pas dans la config caméra). |
| **Save config** | Enregistre ligne + confiance + modèle comme **nouvelle config active**. Déclenche un envoi MQTT `config/saved`. |

### Module 2 — Calcul de cadence (mode intervalle, unique mode)

L'admin définit 3 paramètres au lancement :
- **`interval_minutes`** : période de répétition du cycle (défaut 5 min).
- **`measurement_window_seconds`** : durée d'observation par cycle (défaut 120 s).
- **`anomaly_threshold_pct`** : seuil d'écart pour flagger une anomalie (défaut 15 %).

Comportement par cycle :
1. Le flux s'ouvre pendant la fenêtre de mesure.
2. Chaque objet qui franchit la ligne est horodaté : t0, t1, t2, … (en mémoire, **non stockés bruts**).
3. En fin de fenêtre : écarts consécutifs → moyenne `avg_delta` → **OPM = 60 / avg_delta**.
4. Comparaison à la plage de référence → statut `below` / `normal` / `above`.
5. Détection d'anomalie vs moyenne des itérations précédentes du run.
6. Persistance d'**une itération** (moyenne agrégée seulement).
7. Publication MQTT `cadence/iteration`.
8. Pause `interval_minutes`, puis cycle suivant.

L'admin peut **arrêter** à tout moment (Stop).

### Module 3 — Envoi MQTT vers Odoo

Chaque cadence calculée et chaque config sauvegardée sont poussées vers le broker
MQTT central (convention `Miniros/{factory}/{line}/{machine}/…`), pour consommation
par Odoo au même titre que les nœuds ESP32. **Best-effort** : si le broker est
injoignable, la mesure continue, les données restent en DB.

### Dashboard — Affichage

- Statut caméra + dernier ping.
- Session en cours : statut, timer, OPM courant (dernière itération).
- Session terminée : **moyenne globale** (`avg_opm`) comme métrique principale.
- Liste des OPM par itération avec badge `below`/`normal`/`above` + anomalies.
- Compteurs `below`/`normal`/`above`, historique des sessions (avg/min/max OPM, nb anomalies).

---

## 2. Cas d'utilisation & scénarios utilisateur

### UC-1 — Vérifier qu'une caméra est opérationnelle
**Acteur** : admin. **But** : savoir si la caméra répond avant de mesurer.
**Scénario** : ouvre l'outil → voit le statut → (optionnel) ouvre le preview pour confirmer l'image.

### UC-2 — Configurer la détection
**Scénario** : ouvre le preview → fait glisser la ligne de trigger à l'endroit du passage des objets → règle confiance/modèle → (optionnel) saisit la plage de référence → **Save config**.
**Résultat** : nouvelle config active ; MQTT `config/saved` émis.

### UC-3 — Lancer une mesure de cadence
**Pré-requis** : une config active existe.
**Scénario** : règle `interval_minutes`, `measurement_window_seconds`, seuil d'anomalie → **Launch**.
**Résultat** : session `pending` → `measuring` ; premier OPM affiché en fin de 1ʳᵉ fenêtre.

### UC-4 — Suivre la production en temps réel
**Scénario** : observe la liste des OPM se remplir cycle après cycle, les badges de statut, les anomalies surlignées.

### UC-5 — Arrêter et consulter le bilan
**Scénario** : **Stop** → le gros chiffre bascule sur la **moyenne globale** du run ; la dernière mesure passe en info secondaire.

### UC-6 — Consulter l'historique
**Scénario** : ouvre l'historique → liste des sessions passées avec résumé (avg/min/max OPM, anomalies, plage de réf).

### UC-7 — Intégration Odoo
**Acteur** : système Odoo. **Scénario** : Odoo reçoit chaque `cadence/iteration` sur le topic de la machine et l'exploite comme une donnée de nœud.

---

## 3. Règles métier

1. **Mode unique** : seul le mode *intervalle* existe (le mode « single » a été supprimé).
2. **Une seule config active par caméra** : sauvegarder une config désactive automatiquement la précédente.
3. **Config requise pour mesurer** : lancer une session sans config active est refusé (**409**).
4. **Calcul OPM** : `OPM = 60 / avg_delta_seconds`. Si **moins de 2 objets** franchissent la ligne dans la fenêtre → pas d'écart calculable → `avg_delta` et `OPM` = **NULL**.
5. **Plage de référence** : soit les **deux** bornes fournies (avec `min ≤ max`), soit **aucune**. Statut :
   - `OPM < min` → **below**
   - `min ≤ OPM ≤ max` → **normal**
   - `OPM > max` → **above**
   - pas de plage → statut **NULL** (pas de comparaison).
6. **Anomalie** : une itération est `is_anomaly = true` si son OPM s'écarte de la **moyenne des itérations précédentes du run** de plus de `anomaly_threshold_pct`. Pas d'anomalie sur la 1ʳᵉ itération (pas de baseline).
7. **Cycle de vie d'une session** : `pending → (measuring → paused)* → stopped` ; `failed` en cas d'erreur (caméra perdue, etc.).
8. **Cadence d'affichage des résultats** : 1ᵉʳ résultat après `measurement_window_seconds` ; ensuite un résultat tous les `interval_minutes + measurement_window_seconds`.
9. **Affichage final** : après Stop (ou session terminée), la métrique principale = **moyenne globale** (`avg_opm`).
10. **Données stockées** : uniquement les **moyennes agrégées** par itération, jamais les timestamps bruts des franchissements.
11. **Multi-tenant** : un utilisateur ne voit que les données de **son** organisation (RLS). Écriture réservée aux rôles `admin`/`operator`.
12. **MQTT best-effort** : un échec MQTT n'interrompt jamais la mesure ni n'altère la DB.
13. **Détecteur commutable** : YOLO (précis) ou OpenCV (léger), via configuration, sans changement de comportement métier.

---

## 4. Manuel utilisateur

### 4.1 Préparer une caméra
1. Sélectionner la caméra ; vérifier le **statut** (online).
2. Cliquer **Open stream** pour voir l'image.
3. **Glisser la ligne** verticale là où passent les objets.
4. Régler **confiance** et **modèle** (laisser les défauts si doute).
5. (Optionnel) Saisir la **plage de cadence de référence** (min/max OPM attendus).
6. Cliquer **Save config**.

### 4.2 Lancer une mesure
1. Aller sur la page **Cadence**.
2. Renseigner :
   - **Intervalle** (min) — fréquence des cycles ;
   - **Fenêtre de mesure** (s) — durée d'observation par cycle ;
   - **Seuil d'anomalie** (%).
3. Cliquer **Launch**.
4. Attendre la fin de la 1ʳᵉ fenêtre → le premier OPM s'affiche.

### 4.3 Suivre et arrêter
- Observer la liste des OPM (badges below/normal/above, anomalies).
- Cliquer **Stop** quand voulu → la **moyenne globale** s'affiche en grand.

### 4.4 Consulter l'historique
- Page Historique → sélectionner une session → résumé (avg/min/max OPM, anomalies, plage de réf, compteurs de statut).

### 4.5 Conseils de réglage
- **Fenêtre courte** (ex. 60 s) + **intervalle court** (ex. 1 min) → résultats plus fréquents (utile en test).
- **Mode OPENCV** sur Raspberry Pi → fluide/temps réel ; **YOLO** si besoin de précision/robustesse.

---

## 5. FAQ & gestion des erreurs courantes

**Q. L'interface affiche « CORS error » / preflight 400.**
L'origine du frontend n'est pas autorisée côté backend. Ajouter `http://<ip>:5173` dans `CORS_ORIGINS` (`.env` backend) et relancer. Ne pas utiliser `*` (incompatible avec les credentials).

**Q. L'OPM affiché est NULL.**
Moins de 2 objets ont franchi la ligne pendant la fenêtre → aucun écart calculable. Vérifier la position de la ligne, la durée de fenêtre, et que les objets passent bien.

**Q. Je ne vois pas la détection (boîtes) sur le preview.**
Le preview seul montre le flux brut. Les boîtes/annotations n'apparaissent **que pendant une session lancée** (le preview bascule alors sur les images annotées du runner).

**Q. La cadence mesurée semble trop basse (test sur fichier vidéo).**
Sur un **fichier vidéo** traité plus lentement que le temps réel, l'horodatage (à l'horloge réelle) est étiré → OPM sous-estimé. Phénomène **absent sur une vraie caméra** temps réel.

**Q. La vidéo défile lentement pendant une session.**
Le détecteur (surtout YOLO sur CPU/Pi) traite quelques images/seconde. C'est attendu : la lecture suit la vitesse de calcul. Le comptage et l'OPM restent cohérents (cf. question précédente pour la justesse sur fichier).

**Q. Pourquoi ~9 minutes entre deux résultats ?**
Le 1ᵉʳ résultat tombe après la fenêtre (ex. 4 min) ; les suivants après `intervalle + fenêtre` (ex. 5 + 4 = 9 min). Réduire `interval_minutes` et `measurement_window_seconds` pour accélérer.

**Q. « No active config for this camera » (409) au lancement.**
Aucune config active : faire **Save config** sur la caméra avant de lancer une session.

**Q. Rien n'arrive sur MQTT (`mosquitto_sub` ne montre rien).**
Vérifier : `MQTT_ENABLED=true`, `MQTT_BROKER_HOST` correct (broker réellement joignable), paho-mqtt installé, et écouter sur le **bon broker** (`-h <host>` identique). Voir les logs `MQTT connecté…`.

**Q. Comment passer de YOLO à OpenCV (ou l'inverse) ?**
`DETECTOR_BACKEND=opencv` (ou `yolo`) dans `.env`, puis relancer. Le log `>>> RUNNER START … detector=…` confirme le moteur.

**Q. Comment changer la caméra (vidéo → webcam) ?**
Mettre `cameras.stream_url = '0'` (index webcam) au lieu du chemin du fichier. Aucun changement de code.

**Q. Les messages n'arrivent pas sur le bon nœud Odoo.**
Les segments `factory` (slug orga), `line` (location caméra), `machine` (nom caméra) doivent correspondre **exactement** (casse/espaces) au nœud configuré côté Odoo.

**Q. Après redémarrage du backend, une session reste « measuring »/« paused ».**
Les runners sont in-process : un redémarrage les perd. La session doit être arrêtée/nettoyée manuellement. Ne pas lancer plusieurs workers uvicorn.
