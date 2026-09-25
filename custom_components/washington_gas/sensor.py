"""Sensors for Washington Gas usage and cost."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import TIMEZONE, CostRead
from .const import DOMAIN
from .coordinator import AccountData, WashingtonGasConfigEntry, WashingtonGasCoordinator

PARALLEL_UPDATES = 0

# Which data a sensor needs before it is created.
GROUP_FORECAST = "forecast"
GROUP_DAY = "day"
GROUP_BILL = "bill"

# Home Assistant has no therm unit, so therm sensors use a plain label.
UNIT_THERMS = "therms"


@dataclass(frozen=True, kw_only=True)
class WashingtonGasSensorDescription(SensorEntityDescription):
    """Describes a Washington Gas sensor."""

    group: str
    is_usage: bool = False
    value_fn: Callable[[AccountData], float | date | None]
    attrs_fn: Callable[[AccountData], dict[str, Any] | None] = lambda _: None


def _period_attrs(read: CostRead | None) -> dict[str, Any] | None:
    if read is None:
        return None
    return {
        "period_start": read.start_time.astimezone(TIMEZONE).date().isoformat(),
        "period_end": read.end_time.astimezone(TIMEZONE).date().isoformat(),
    }


def _forecast_value(field: str) -> Callable[[AccountData], float | date | None]:
    def value(data: AccountData) -> float | date | None:
        if data.forecast is None:
            return None
        result: float | date | None = getattr(data.forecast, field)
        return result

    return value


def _usage(
    key: str, group: str, value_fn: Callable[[AccountData], float | date | None], **kwargs: Any
) -> WashingtonGasSensorDescription:
    return WashingtonGasSensorDescription(
        key=key,
        translation_key=key,
        group=group,
        is_usage=True,
        suggested_display_precision=1,
        value_fn=value_fn,
        **kwargs,
    )


def _cost(
    key: str, group: str, value_fn: Callable[[AccountData], float | date | None], **kwargs: Any
) -> WashingtonGasSensorDescription:
    return WashingtonGasSensorDescription(
        key=key,
        translation_key=key,
        group=group,
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="USD",
        suggested_display_precision=2,
        value_fn=value_fn,
        **kwargs,
    )


SENSORS: tuple[WashingtonGasSensorDescription, ...] = (
    # The bill period in progress, from Opower's bill forecast.
    _usage("current_bill_usage", GROUP_FORECAST, _forecast_value("usage_to_date"), state_class=SensorStateClass.TOTAL),
    _cost("current_bill_cost", GROUP_FORECAST, _forecast_value("cost_to_date"), state_class=SensorStateClass.TOTAL),
    _usage(
        "forecasted_bill_usage", GROUP_FORECAST, _forecast_value("forecasted_usage"), state_class=SensorStateClass.TOTAL
    ),
    _cost(
        "forecasted_bill_cost", GROUP_FORECAST, _forecast_value("forecasted_cost"), state_class=SensorStateClass.TOTAL
    ),
    _usage("typical_bill_usage", GROUP_FORECAST, _forecast_value("typical_usage"), state_class=SensorStateClass.TOTAL),
    _cost("typical_bill_cost", GROUP_FORECAST, _forecast_value("typical_cost"), state_class=SensorStateClass.TOTAL),
    WashingtonGasSensorDescription(
        key="bill_period_start",
        translation_key="bill_period_start",
        group=GROUP_FORECAST,
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_forecast_value("start_date"),
    ),
    WashingtonGasSensorDescription(
        key="bill_period_end",
        translation_key="bill_period_end",
        group=GROUP_FORECAST,
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_forecast_value("end_date"),
    ),
    # The newest full day Washington Gas has published (usually 1 to 2 days ago).
    _usage(
        "latest_day_usage",
        GROUP_DAY,
        lambda data: data.latest_day.consumption if data.latest_day else None,
        attrs_fn=lambda data: _period_attrs(data.latest_day),
    ),
    _cost(
        "latest_day_cost",
        GROUP_DAY,
        lambda data: data.latest_day.cost if data.latest_day else None,
        attrs_fn=lambda data: _period_attrs(data.latest_day),
    ),
    # The most recent completed bill.
    _usage(
        "last_bill_usage",
        GROUP_BILL,
        lambda data: data.latest_bill.consumption if data.latest_bill else None,
        attrs_fn=lambda data: _period_attrs(data.latest_bill),
    ),
    _cost(
        "last_bill_cost",
        GROUP_BILL,
        lambda data: data.latest_bill.cost if data.latest_bill else None,
        attrs_fn=lambda data: _period_attrs(data.latest_bill),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WashingtonGasConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors for each account."""
    coordinator = entry.runtime_data
    entities: list[WashingtonGasSensor] = []
    for account_id, data in coordinator.data.items():
        groups = {GROUP_BILL}
        if data.forecast is not None:
            groups.add(GROUP_FORECAST)
        if data.account.supports("day"):
            groups.add(GROUP_DAY)
        entities.extend(
            WashingtonGasSensor(coordinator, description, account_id, data)
            for description in SENSORS
            if description.group in groups
        )
    async_add_entities(entities)


class WashingtonGasSensor(CoordinatorEntity[WashingtonGasCoordinator], SensorEntity):
    """A Washington Gas usage or cost sensor."""

    _attr_has_entity_name = True
    entity_description: WashingtonGasSensorDescription

    def __init__(
        self,
        coordinator: WashingtonGasCoordinator,
        description: WashingtonGasSensorDescription,
        account_id: str,
        data: AccountData,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._account_id = account_id
        self._attr_unique_id = f"{account_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, account_id)},
            name=f"Washington Gas {data.account.utility_account_id}",
            manufacturer="Washington Gas",
            model="Gas account (via Opower)",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=f"https://{coordinator.client.portal.subdomain}.opower.com/ei/x/home-energy-analysis"
            if coordinator.client.portal
            else None,
        )
        if description.is_usage:
            if data.unit == "CCF":
                self._attr_device_class = SensorDeviceClass.GAS
                self._attr_native_unit_of_measurement = UnitOfVolume.CENTUM_CUBIC_FEET
            else:
                self._attr_native_unit_of_measurement = UNIT_THERMS

    @property
    def _data(self) -> AccountData | None:
        return self.coordinator.data.get(self._account_id) if self.coordinator.data else None

    @property
    def available(self) -> bool:
        """Return True while the account is still on the login."""
        return super().available and self._data is not None

    @property
    def native_value(self) -> float | date | None:
        """Return the current value."""
        data = self._data
        return self.entity_description.value_fn(data) if data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return which period the value covers, where that applies."""
        data = self._data
        return self.entity_description.attrs_fn(data) if data else None
