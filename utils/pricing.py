from dataclasses import dataclass
from settings.database import AsyncSessionLocal
from settings.macro import DOR2CHY, PROFIT_MULTIPLIER
from settings.models import PricingSettings


@dataclass(frozen=True)
class PricingConfig:
    usd_cny_rate: float
    profit_multiplier: float


def _default_config() -> PricingConfig:
    return PricingConfig(float(DOR2CHY), float(PROFIT_MULTIPLIER))


async def get_pricing_config(db=None) -> PricingConfig:
    """Read singleton config; fall back to legacy constants if DB is unavailable/unseeded."""
    try:
        if db is not None:
            row = await db.get(PricingSettings, 1)
            if row:
                return PricingConfig(row.usd_cny_rate, row.profit_multiplier)
        else:
            async with AsyncSessionLocal() as session:
                row = await session.get(PricingSettings, 1)
                if row:
                    return PricingConfig(row.usd_cny_rate, row.profit_multiplier)
    except Exception:
        # Billing remains available during a transient settings-table/database issue.
        return _default_config()
    return _default_config()


async def ensure_pricing_settings():
    """Create the singleton table and seed the initial settings idempotently."""
    async with AsyncSessionLocal() as db:
        row = await db.get(PricingSettings, 1)
        if row is None:
            row = PricingSettings(
                id=1,
                usd_cny_rate=float(DOR2CHY),
                profit_multiplier=float(PROFIT_MULTIPLIER),
            )
            db.add(row)
            await db.commit()
        return PricingConfig(row.usd_cny_rate, row.profit_multiplier)


async def update_pricing_settings(db, *, usd_cny_rate: float, profit_multiplier: float, updated_by: int):
    row = await db.get(PricingSettings, 1)
    if row is None:
        row = PricingSettings(id=1)
        db.add(row)
    row.usd_cny_rate = usd_cny_rate
    row.profit_multiplier = profit_multiplier
    row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)
    return PricingConfig(row.usd_cny_rate, row.profit_multiplier)
