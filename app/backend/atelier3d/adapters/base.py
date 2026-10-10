"""Interface commune des adapters de moteurs (capabilities, validate_input, run, cancel, collect_artifacts)."""
from __future__ import annotations

import abc
from typing import Any


class Adapter(abc.ABC):
    name: str = "adapter"

    @abc.abstractmethod
    def capabilities(self) -> dict:
        """État réel : {capacité: {status: disponible|à tester|indisponible, engine, versions, formats, ressources, limites, reason}}."""

    def validate_input(self, ctx) -> list[str]:  # pragma: no cover - surchargé
        return []

    @abc.abstractmethod
    def run(self, ctx) -> dict:
        """Exécute l'étape ; retourne {outputs: {port: [artifact_id]}, engine: {...}, stats: {...}}."""

    def cancel(self, ctx) -> bool:  # pragma: no cover - surchargé
        return False

    def collect_artifacts(self, ctx) -> dict | None:  # pragma: no cover - surchargé
        """Reprise : récupère les sorties d'un calcul déjà soumis (sans resoumettre)."""
        return None


class StepFailure(Exception):
    def __init__(self, message: str, *, code: str = "step_failed", oom: bool = False, details: Any = None, corrective: str | None = None):
        super().__init__(message)
        self.code, self.oom, self.details, self.corrective = code, oom, details, corrective

    def to_json(self) -> dict:
        return {"code": self.code, "message": str(self), "oom": self.oom, "details": self.details, "corrective_action": self.corrective}


class Cancelled(Exception):
    pass
