"""Room thermostat for the main zone (for example, the living room)."""

import math

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..common import get_register_value, is_entity_available, safe_write_register
from ..core.register_constants import CALCULATED_DEVICE_INFO


class DaikinRoomThermostatClimate(CoordinatorEntity, ClimateEntity):
    """Control the actual room setpoint, independently of water offsets."""

    _attr_has_entity_name = True
    _attr_log_when_unavailable = False
    _attr_translation_key = "daikin_room_thermostat_climate"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_device_info = CALCULATED_DEVICE_INFO

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_hvac_modes = [
            HVACMode.OFF,
            HVACMode.HEAT,
            HVACMode.COOL,
            HVACMode.AUTO,
        ]
        self._attr_unique_id = f"{entry.entry_id}_room_thermostat_main"

    def _value(self, register):
        # Coordinator payloads have already been scaled to engineering units.
        if not is_entity_available(self.coordinator.data or {}, register):
            return None
        return get_register_value(self.coordinator.data[register])

    @property
    def _cooling(self):
        mode = self._value("holding_3")
        return mode == 2 or (mode == 0 and self._value("input_38") == 2)

    @property
    def _setpoint_register(self):
        fine, legacy = (
            ("holding_77", "holding_7")
            if self._cooling
            else ("holding_76", "holding_6")
        )
        return fine if self._value(fine) is not None else legacy

    @property
    def available(self):
        return super().available and all(
            self._value(register) is not None
            for register in (
                "input_50",
                "holding_3",
                "holding_4",
                "coil_2",
                self._setpoint_register,
            )
        )

    @property
    def current_temperature(self):
        return self._value("input_50")

    @property
    def target_temperature(self):
        return self._value(self._setpoint_register)

    @property
    def target_temperature_step(self):
        return 0.1 if self._setpoint_register in ("holding_76", "holding_77") else 1

    @property
    def min_temp(self):
        value = self._value("input_86" if self._cooling else "input_84")
        return value if value is not None else 12

    @property
    def max_temp(self):
        value = self._value("input_87" if self._cooling else "input_85")
        return value if value is not None else (35 if self._cooling else 30)

    @property
    def hvac_mode(self):
        if self._value("coil_2") == 0 or self._value("holding_4") == 0:
            return HVACMode.OFF
        return {0: HVACMode.AUTO, 1: HVACMode.HEAT, 2: HVACMode.COOL}.get(
            self._value("holding_3")
        )

    @property
    def hvac_action(self):
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        # Compressor activity also includes DHW: use the main-zone flag instead.
        running = self._value("discrete_20")
        mode = self._value("input_38")
        if running is None or mode is None:
            return None
        if running == 0:
            return HVACAction.IDLE
        return {1: HVACAction.HEATING, 2: HVACAction.COOLING}.get(mode, HVACAction.IDLE)

    async def _write(self, register, value, *, coil=False):
        if self._value(register) is None:
            raise HomeAssistantError(
                f"Room thermostat register {register} is unavailable"
            )
        write = (
            self.coordinator.data_manager.write_coil_register
            if coil
            else self.coordinator.data_manager.write_holding_register
        )
        await safe_write_register(
            write,
            register,
            value,
            operation_name="set",
            register_type="room thermostat",
            coordinator=self.coordinator,
        )

    async def async_set_temperature(self, **kwargs):
        temperature = kwargs.get("temperature")
        if temperature is None:
            return
        temperature = float(temperature)
        if not math.isfinite(temperature):
            raise HomeAssistantError("Room temperature must be finite")
        register = self._setpoint_register
        temperature = max(self.min_temp, min(self.max_temp, temperature))
        step = self.target_temperature_step
        temperature = round(round(temperature / step) * step, 2)
        scale = 0.01 if register in ("holding_76", "holding_77") else 1
        await self._write(register, round(temperature / scale))

    async def async_set_hvac_mode(self, hvac_mode):
        if hvac_mode == HVACMode.OFF:
            # Only switch the main zone off; preserve DHW and the additional zone.
            await self._write("coil_2", 0, coil=True)
            return
        modes = {HVACMode.AUTO: 0, HVACMode.HEAT: 1, HVACMode.COOL: 2}
        if hvac_mode not in modes:
            raise HomeAssistantError(f"Unsupported room thermostat mode: {hvac_mode}")
        # Heating-only models report holding_3 as 32766 and cannot use this entity.
        await self._write("holding_3", modes[hvac_mode])
        await self._write("holding_4", 1)
        await self._write("coil_2", 1, coil=True)

    async def async_turn_on(self):
        # Preserve the selected heating/cooling mode.
        await self._write("holding_4", 1)
        await self._write("coil_2", 1, coil=True)

    async def async_turn_off(self):
        await self.async_set_hvac_mode(HVACMode.OFF)
