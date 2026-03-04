from __future__ import annotations

import json
import os
from pathlib import Path

from .models import Portfolio, portfolio_from_dict, portfolio_to_dict


class StateManager:
    def __init__(self, state_file: str | Path = "paper_trader_state.json") -> None:
        self._path = Path(state_file)

    def load(self) -> Portfolio:
        if not self._path.exists():
            return Portfolio()
        with self._path.open() as f:
            data = json.load(f)
        return portfolio_from_dict(data)

    def save(self, portfolio: Portfolio) -> None:
        tmp = self._path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(portfolio_to_dict(portfolio), f, indent=2)
        os.replace(tmp, self._path)
