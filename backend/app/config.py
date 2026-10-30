"""Configuracao via variaveis de ambiente (pydantic-settings).

NUNCA hardcodar senhas — ROS_PASS e afins vem do .env / EnvironmentFile.
"""
from functools import lru_cache
from typing import List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class RouterTarget:
    """Descricao estatica de um roteador monitorado."""

    def __init__(
        self,
        name: str,
        host: str,
        label: str,
        lan_subnet: str,
        wan_interface: str,
        transit_interface: str,
        wan_gateway: str,
        isp_name: str,
        route_comment_hints: List[str],
    ) -> None:
        self.name = name                # identificador interno, ex.: "mk-a"
        self.host = host                # IP/DNS alcancavel pelo backend
        self.label = label              # nome amigavel na UI
        self.lan_subnet = lan_subnet
        self.wan_interface = wan_interface
        self.transit_interface = transit_interface
        self.wan_gateway = wan_gateway  # alvo do ping de RTT do link
        self.isp_name = isp_name
        # comments conhecidos das rotas default que o script FAILOVER liga/desliga
        self.route_comment_hints = route_comment_hints


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Roteadores ---
    mk_a_host: str = "10.2.2.1"
    mk_b_host: str = "10.3.3.1"
    ros_user: str = "netagent"
    ros_pass: str = ""                      # obrigatorio via env — nunca default real
    ros_port: int = 8729
    ros_use_ssl: bool = True
    ros_ssl_verify: bool = False            # cert auto-assinado do roteador (ver .rsc de setup)
    ros_connect_timeout: float = 5.0

    # --- Alertas ---
    ntfy_url: str = "https://ntfy.example.com/alertas"
    ntfy_enabled: bool = True           # false desliga TODO push ntfy
    ntfy_min_severity: str = "warning"  # piso p/ regras sem flag notify

    # --- Poller ---
    poll_interval: float = 3.0              # segundos entre ciclos de coleta
    ping_count: int = 3                     # pings por medicao de RTT do gateway

    # --- Storage ---
    db_path: str = "./netagent.db"
    sample_retention_hours: int = 24        # samples crus
    rollup_retention_days: int = 30         # rollups de 1 min

    # --- Agente / seguranca ---
    agent_read_only: bool = False           # kill-switch inicial (pode ligar via env)
    alert_dedup_minutes: int = 10

    # --- LLM do agente ---
    # backend: "anthropic" (Messages API) ou "openai" (endpoint compativel
    # /v1/chat/completions — Ollama, LM Studio, vLLM etc.)
    llm_backend: str = "openai"
    llm_base_url: str = ""                  # ex.: http://10.3.3.11:11434/v1
    llm_model: str = ""                     # vazio -> anthropic_model
    llm_api_key: str = ""                   # openai/ollama pode ser qualquer str

    # --- Agente de IA (Fase 3 — Claude via httpx puro) ---
    anthropic_api_key: str = ""             # vazio = agente idle (sem chamadas)
    anthropic_model: str = "claude-sonnet-4-6"
    agent_interval_s: int = 300             # segundos entre ciclos de analise
    agent_enabled: bool = True              # false desliga o loop de analise

    # --- Wi-Fi (AP Tenda RX2 Pro, LAN da Casa User) ---
    tenda_host: str = "10.3.3.178"       # webui local do AP
    tenda_pass: str = ""                    # senha admin do webui (vazio = sem login)
    wifi_poll_interval: float = 15.0        # segundos entre consultas ao Tenda

    # --- AdGuard Home (CT109, DNS da rede) ---
    adguard_url: str = "https://10.3.3.2:3002"  # api /control/*
    adguard_user: str = ""
    adguard_pass: str = ""
    adguard_poll_interval: float = 30.0

    # --- IPFIX ---
    ipfix_listen_host: str = "0.0.0.0"
    ipfix_listen_port: int = 2055
    # csv de sub-redes "locais" p/ agregacao dos top talkers
    lan_subnets: str = "10.2.2.0/24,10.3.3.0/24,172.20.20.0/30"

    # --- Allowlist de acoes autonomas (csv de kinds, ex.: "toggle_route") ---
    action_allowlist: str = ""              # vazio = nada autonomo, tudo vira proposta

    def routers(self) -> List[RouterTarget]:
        """Topologia conhecida da rede (veja SPEC.md)."""
        return [
            RouterTarget(
                name="mk-a",
                host=self.mk_a_host,
                label="hEX — Casa Mae",
                lan_subnet="10.2.2.0/24",
                wan_interface="Link 2 - RedeBR",
                transit_interface="ether4-LAN-ROD",
                wan_gateway="10.1.1.254",
                isp_name="RedeBR",
                route_comment_hints=["LINK REDEBR", "LINK 2 #6", "LINK 2"],
            ),
            RouterTarget(
                name="mk-b",
                host=self.mk_b_host,
                label="house-b — Casa User",
                lan_subnet="10.3.3.0/24",
                wan_interface="ether1-ONDALIVRE",
                transit_interface="ether5-TRANSITO-MAE",
                wan_gateway="10.1.1.1",
                isp_name="Onda Livre",
                route_comment_hints=["LINK ONDA-LIVRE", "LINK 1 #3", "LINK 1"],
            ),
        ]

    def allowlist(self) -> List[str]:
        return [k.strip() for k in self.action_allowlist.split(",") if k.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
