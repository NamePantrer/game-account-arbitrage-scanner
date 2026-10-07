from __future__ import annotations

import abc
import logging
from typing import Optional

from src.models import Listing

logger = logging.getLogger(__name__)


class BaseParser(abc.ABC):
  platform_name: str

  @abc.abstractmethod
  def fetch_listings(self, game: str, category: str, **kwargs) -> list[Listing]:
    raise NotImplementedError
