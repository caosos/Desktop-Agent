"""Run the control plane: `python -m desktop_agent [--config path]`."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from .control.api import build_app
from .control.config import RuntimeConfig
from .control.service import Service


def main() -> None:
    ap = argparse.ArgumentParser(prog="desktop-agent")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--check-providers", action="store_true",
                    help="report provider configuration and list OpenAI model ids (free call); no completions")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = RuntimeConfig.load(args.config)
    if args.check_providers:
        import json as _json
        from .control.llm import LLM, LLMError, list_openai_models
        llm = LLM(cfg)
        print(_json.dumps(llm.status(), indent=2))
        if cfg.provider_key("openai"):
            try:
                ids = list_openai_models(cfg)
                print(_json.dumps({"openai_models_available": len(ids), "ids": ids}, indent=2))
            except LLMError as exc:
                print(_json.dumps({"openai_models_error": str(exc)}))
        return
    token = cfg.ensure_token()
    service = Service(cfg)
    app = build_app(service, token)
    port = args.port or cfg.api_port
    logging.getLogger("desktop_agent").info("control plane on http://%s:%d  projects=%s  token=%s",
                                            cfg.api_host, port, list(service.projects), cfg.token_file)
    uvicorn.run(app, host=cfg.api_host, port=port, log_level="info")


if __name__ == "__main__":
    main()
