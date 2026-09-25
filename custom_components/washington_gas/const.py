"""Constants for the Washington Gas integration."""

DOMAIN = "washington_gas"

CONF_PORTAL_SUBDOMAIN = "portal_subdomain"
CONF_PORTAL_UTILITY_CODE = "portal_utility_code"
CONF_ENERGY_UNIT = "energy_unit"

# How gas usage is written to the Energy dashboard statistics.
# Home Assistant has no therm unit. "ccf" records the therm numbers from the
# bill as-is under the CCF label (what the built-in Opower integration does).
# "kwh" converts therms to kWh exactly.
ENERGY_UNIT_CCF = "ccf"
ENERGY_UNIT_KWH = "kwh"
DEFAULT_ENERGY_UNIT = ENERGY_UNIT_CCF

# 1 therm is 100,000 BTU, and 1 BTU is 0.29307107017 Wh.
KWH_PER_THERM = 29.307107017
