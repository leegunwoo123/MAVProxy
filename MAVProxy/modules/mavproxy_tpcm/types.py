"""TPCM ASCII frame types (no MAVProxy dependency).

AP_FLAKE8_CLEAN
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TpcmStatus:
    ac_input_voltage_1: float
    ac_input_voltage_2: float
    ac_input_voltage_3: float
    dc_output_voltage_1: float
    dc_output_voltage_2: float
    dc_output_voltage_3: float
    dc_output_current_1: float
    dc_output_current_2: float
    dc_output_current_3: float
    temperature: float
    battery_voltage: float
    power_source: int

    def as_dict(self) -> dict:
        return {
            "ac_input_voltage_1": self.ac_input_voltage_1,
            "ac_input_voltage_2": self.ac_input_voltage_2,
            "ac_input_voltage_3": self.ac_input_voltage_3,
            "dc_output_voltage_1": self.dc_output_voltage_1,
            "dc_output_voltage_2": self.dc_output_voltage_2,
            "dc_output_voltage_3": self.dc_output_voltage_3,
            "dc_output_current_1": self.dc_output_current_1,
            "dc_output_current_2": self.dc_output_current_2,
            "dc_output_current_3": self.dc_output_current_3,
            "temperature": self.temperature,
            "battery_voltage": self.battery_voltage,
            "power_source": self.power_source,
        }


@dataclass(frozen=True)
class ParseError:
    reason: str
