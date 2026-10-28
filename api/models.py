from typing import Optional
from pydantic import BaseModel


class PickOut(BaseModel):
    id: int
    mode: str
    league: str
    home: str
    away: str
    market: str
    selection: str
    line: Optional[float] = None
    book: Optional[str] = None
    book_odds: Optional[float] = None
    fair_odds: float
    min_odds: float
    ev: float
    stake: int
    status: str


class ReadyOut(BaseModel):
    status: str
    db: bool
    gold_pinnacle_rows: int
    scanner: bool
