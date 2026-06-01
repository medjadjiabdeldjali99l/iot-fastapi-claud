"""Détecteur OpenCV pur — alternative à YOLO, même interface que `YoloDetector`.

But : mesurer la cadence **sans réseau de neurones**, pour tester l'optimisation
sur Raspberry Pi (YOLO sur CPU plafonne à ~1-3 fps ; ici on vise le temps réel).

Pipeline classique :

1. **Soustraction de fond** (MOG2) → masque des pixels en mouvement.
2. **Nettoyage** : suppression des ombres + morphologie (open/close).
3. **Contours** (`findContours`) → boîtes englobantes filtrées par aire.
4. **Centroid tracker maison** → attribue un `tracker_id` stable d'une frame à
   l'autre (indispensable pour que `LineCrosser` compte chaque objet une seule
   fois et que les timestamps de cadence soient justes).

L'interface est **strictement identique** à `YoloDetector` :

    async def track(frame, confidence) -> list[Detection]

`Detection` est le MÊME dataclass que pour YOLO (importé tel quel), donc ce
détecteur se branche dans `SessionRunner` / `LineCrosser` sans rien changer en
aval. Le `confidence` est accepté pour compat mais ignoré (pas de score en
vision classique) ; `class_name` est fixe ("object") car OpenCV ne reconnaît
pas la nature de l'objet, il le détecte seulement.

Note : MOG2 et le tracker sont **stateful**. Comme pour YOLO, on crée une
instance neuve par itération de mesure → fond et IDs repartent à zéro à chaque
fenêtre.
"""
from __future__ import annotations

import asyncio
from threading import Lock

import cv2
import numpy as np

from app.services.detection import Detection


class _CentroidTracker:
    """Tracker par plus-proche-centroïde (variante classique « pyimagesearch »).

    Associe les détections d'une frame aux objets de la frame précédente par
    distance minimale entre centres de boîtes. Donne un `id` entier stable tant
    que l'objet reste visible ; un objet absent plus de `max_disappeared`
    frames est oublié. Ne fait AUCune reconnaissance — juste de la continuité
    spatiale, suffisante pour une chaîne où les objets défilent régulièrement.
    """

    def __init__(self, max_disappeared: int = 30, max_distance: float = 80.0) -> None:
        self._next_id = 0
        self.objects: dict[int, tuple[float, float]] = {}
        self.disappeared: dict[int, int] = {}
        self.max_disappeared = max_disappeared
        self.max_distance = max_distance

    def _register(self, centroid: tuple[float, float]) -> int:
        oid = self._next_id
        self.objects[oid] = centroid
        self.disappeared[oid] = 0
        self._next_id += 1
        return oid

    def _deregister(self, oid: int) -> None:
        self.objects.pop(oid, None)
        self.disappeared.pop(oid, None)

    def update(self, boxes: list[tuple[float, float, float, float]]) -> list[int]:
        """Renvoie la liste des `tracker_id` parallèle à `boxes` (même ordre)."""
        centroids = [
            ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0) for b in boxes
        ]

        # Aucune détection cette frame : tout le monde « disparaît » d'un cran.
        if not centroids:
            for oid in list(self.disappeared.keys()):
                self.disappeared[oid] += 1
                if self.disappeared[oid] > self.max_disappeared:
                    self._deregister(oid)
            return []

        # Aucun objet connu : on enregistre tout.
        if not self.objects:
            return [self._register(c) for c in centroids]

        object_ids = list(self.objects.keys())
        object_centroids = np.array(
            [self.objects[oid] for oid in object_ids], dtype=float
        )
        input_centroids = np.array(centroids, dtype=float)

        # Matrice de distances (objets connus × détections nouvelles).
        dist = np.linalg.norm(
            object_centroids[:, None] - input_centroids[None, :], axis=2
        )

        assigned: list[int | None] = [None] * len(centroids)
        used_rows: set[int] = set()
        used_cols: set[int] = set()

        # Appariement glouton par distance croissante.
        rows = dist.min(axis=1).argsort()
        cols = dist.argmin(axis=1)[rows]
        for row, col in zip(rows.tolist(), cols.tolist()):
            if row in used_rows or col in used_cols:
                continue
            if dist[row, col] > self.max_distance:
                continue
            oid = object_ids[row]
            self.objects[oid] = centroids[col]
            self.disappeared[oid] = 0
            assigned[col] = oid
            used_rows.add(row)
            used_cols.add(col)

        # Objets connus non appariés → on incrémente leur compteur d'absence.
        for row, oid in enumerate(object_ids):
            if row not in used_rows:
                self.disappeared[oid] += 1
                if self.disappeared[oid] > self.max_disappeared:
                    self._deregister(oid)

        # Détections non appariées → nouveaux objets.
        for col in range(len(centroids)):
            if col not in used_cols:
                assigned[col] = self._register(centroids[col])

        return [int(a) for a in assigned if a is not None]


class OpenCvDetector:
    """Détecteur OpenCV, interface compatible `YoloDetector`.

    Paramètres réglables (tunables sans toucher au reste du système) :

    - ``min_area`` / ``max_area`` : surface min/max d'un contour en px² pour le
      garder (filtre le bruit et les blobs aberrants).
    - ``history`` / ``var_threshold`` / ``detect_shadows`` : réglages MOG2.
    - ``max_disappeared`` / ``max_distance`` : réglages du centroid tracker.
    """

    def __init__(
        self,
        min_area: int = 500,
        max_area: int | None = None,
        history: int = 500,
        var_threshold: float = 16.0,
        detect_shadows: bool = True,
        max_disappeared: int = 30,
        max_distance: float = 80.0,
        class_name: str = "object",
    ) -> None:
        self.min_area = min_area
        self.max_area = max_area
        self.class_name = class_name
        self._bg = cv2.createBackgroundSubtractorMOG2(
            history=history,
            varThreshold=var_threshold,
            detectShadows=detect_shadows,
        )
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self._tracker = _CentroidTracker(max_disappeared, max_distance)
        # MOG2 + tracker sont stateful ; on sérialise les appels comme le fait
        # YoloDetector autour de son modèle.
        self._lock = Lock()

    # ------------------------------------------------------------------
    # Cœur : détection sur une frame
    # ------------------------------------------------------------------

    def _foreground_boxes(
        self, frame: np.ndarray
    ) -> list[tuple[float, float, float, float]]:
        """MOG2 → masque nettoyé → contours filtrés → boîtes (x1,y1,x2,y2)."""
        mask = self._bg.apply(frame)
        # MOG2 marque le fond=0, l'ombre=127, l'avant-plan=255. On ne garde que
        # l'avant-plan franc (>200) pour jeter les ombres.
        _, mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._kernel)

        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        boxes: list[tuple[float, float, float, float]] = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < self.min_area:
                continue
            if self.max_area is not None and area > self.max_area:
                continue
            x, y, w, h = cv2.boundingRect(c)
            boxes.append((float(x), float(y), float(x + w), float(y + h)))
        return boxes

    def detect_sync(
        self, frame: np.ndarray, confidence: float = 0.5
    ) -> list[Detection]:
        """Détection sans tracking (tracker_id=None). `confidence` ignoré."""
        with self._lock:
            boxes = self._foreground_boxes(frame)
        return [
            Detection(
                tracker_id=None,
                bbox=box,
                confidence=1.0,
                class_id=0,
                class_name=self.class_name,
            )
            for box in boxes
        ]

    def track_sync(
        self, frame: np.ndarray, confidence: float = 0.5
    ) -> list[Detection]:
        """Détection + tracking : peuple `tracker_id`. `confidence` ignoré
        (pas de score en vision classique). Équivalent de `YoloDetector.track_sync`."""
        with self._lock:
            boxes = self._foreground_boxes(frame)
            ids = self._tracker.update(boxes)
        return [
            Detection(
                tracker_id=tid,
                bbox=box,
                confidence=1.0,
                class_id=0,
                class_name=self.class_name,
            )
            for box, tid in zip(boxes, ids)
        ]

    # ------------------------------------------------------------------
    # Wrappers async — identiques à YoloDetector (exécution en thread)
    # ------------------------------------------------------------------

    async def detect(
        self, frame: np.ndarray, confidence: float = 0.5
    ) -> list[Detection]:
        return await asyncio.to_thread(self.detect_sync, frame, confidence)

    async def track(
        self, frame: np.ndarray, confidence: float = 0.5
    ) -> list[Detection]:
        return await asyncio.to_thread(self.track_sync, frame, confidence)
