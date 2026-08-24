from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class JetaxClient:
    razao_social: str
    cnpj: str
    cidade: str = ""
    tributacao: str = ""
    status: str = ""
    tem_certificado: bool = False
    client_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class MatchResult:
    """Uma decisão explicável da conciliação.

    Somente ``pronto`` é elegível para envio. Correspondências por nome ou por
    CNPJ presente apenas no nome do arquivo ficam em ``revisao_manual``.
    """

    status: str
    cliente: JetaxClient | None = None
    cert: Any | None = None
    metodo: str = ""
    confianca: float = 0.0
    motivo: str = ""
    evidencias: list[str] = field(default_factory=list)

    @property
    def pode_enviar(self) -> bool:
        return (
            self.status == "pronto"
            and self.cliente is not None
            and self.cert is not None
            and self.metodo == "cnpj_certificado"
            and self.confianca >= 99.0
        )


@dataclass
class PipelineResult:
    certificados: list[Any]
    clientes_sem: list[JetaxClient]
    clientes_com: list[JetaxClient]
    matches: list[MatchResult]
    temp_dir: str = ""
    stats: dict[str, int] = field(default_factory=dict)
    safety_ok: bool = True
    safety_message: str = ""
    # Mantido para compatibilidade; contém somente hash de PFX/P12.
    source_manifest: dict[str, str] = field(default_factory=dict)
    # Inventário integral e relativo da árvore configurada.
    source_inventory: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_root: str = ""
    documents: list[Any] = field(default_factory=list)
    excel_findings: list[dict[str, Any]] = field(default_factory=list)
    integrity_changes: list[dict[str, Any]] = field(default_factory=list)
    checkpoint_path: str = ""
    output_dir: str = ""
