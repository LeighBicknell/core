"""Service calls for the Tesla Fleet integration."""

from datetime import time
from typing import TYPE_CHECKING, Any

import probatio
from tesla_fleet_api.const import Scope, VehicleDataEndpoint

from homeassistant.const import (
    ATTR_ID,
    ATTR_LOCATION,
    ATTR_NAME,
    CONF_DEVICE_ID,
    CONF_LATITUDE,
    CONF_LONGITUDE,
)
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    service,
)

from .const import DOMAIN
from .helpers import handle_command, handle_vehicle_command, wake_up_vehicle
from .models import TeslaFleetVehicleData

if TYPE_CHECKING:
    from . import TeslaFleetConfigEntry

# Attributes
ATTR_DESTINATION = "destination"
ATTR_GPS = "gps"
ATTR_PLACE_IDS = "place_ids"
ATTR_DAYS_OF_WEEK = "days_of_week"
ATTR_ENABLE = "enable"
ATTR_START_TIME = "start_time"
ATTR_END_TIME = "end_time"
ATTR_PRECONDITION_TIME = "precondition_time"
ATTR_ONE_TIME = "one_time"

# Services
SERVICE_NAVIGATE_TO_DESTINATION = "navigate_to_destination"
SERVICE_NAVIGATE_TO_COORDINATES = "navigate_to_coordinates"
SERVICE_NAVIGATE_TO_WAYPOINTS = "navigate_to_waypoints"
SERVICE_GET_SCHEDULES = "get_schedules"
SERVICE_ADD_CHARGE_SCHEDULE = "add_charge_schedule"
SERVICE_REMOVE_CHARGE_SCHEDULE = "remove_charge_schedule"
SERVICE_ADD_PRECONDITION_SCHEDULE = "add_precondition_schedule"
SERVICE_REMOVE_PRECONDITION_SCHEDULE = "remove_precondition_schedule"

# Scopes that allow each kind of action, and the error raised when none is granted
VEHICLE_CMDS_SCOPES = (Scope.VEHICLE_CMDS,)
VEHICLE_CHARGING_CMDS_SCOPES = (Scope.VEHICLE_CMDS, Scope.VEHICLE_CHARGING_CMDS)
MISSING_SCOPE_TRANSLATION_KEYS = {
    VEHICLE_CMDS_SCOPES: "missing_scope_vehicle_cmds",
    VEHICLE_CHARGING_CMDS_SCOPES: "missing_scope_vehicle_charging_cmds",
}

# Tesla's day-of-week bitmask, used by signed commands
DAY_OF_WEEK_BITS = {
    "sunday": 1,
    "monday": 2,
    "tuesday": 4,
    "wednesday": 8,
    "thursday": 16,
    "friday": 32,
    "saturday": 64,
}

SCHEDULE_ID = probatio.All(probatio.Coerce(int), probatio.Range(min=1))
DAYS_OF_WEEK = probatio.All(
    probatio.EnsureList(), [probatio.In(DAY_OF_WEEK_BITS)], probatio.Length(min=1)
)
LOCATION = {
    probatio.Required(CONF_LATITUDE): cv.latitude,
    probatio.Required(CONF_LONGITUDE): cv.longitude,
}


def async_get_vehicle_for_service_call(
    hass: HomeAssistant,
    call: ServiceCall,
    required_scopes: tuple[Scope, ...] | None = VEHICLE_CMDS_SCOPES,
) -> TeslaFleetVehicleData:
    """Get the vehicle targeted by a service call and check its scopes.

    The call is allowed when any of the required scopes has been granted.
    """
    device: dr.DeviceEntry
    config: TeslaFleetConfigEntry
    # Vehicles are matched by serial number, which only a main device has
    device, config = service.async_get_device_and_config_entry(
        hass, DOMAIN, call.data[CONF_DEVICE_ID], include_child_devices=False
    )
    if required_scopes and not any(
        scope in config.runtime_data.scopes for scope in required_scopes
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=MISSING_SCOPE_TRANSLATION_KEYS[required_scopes],
        )
    for vehicle in config.runtime_data.vehicles:
        if vehicle.vin == device.serial_number:
            return vehicle
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="no_vehicle_data_for_device",
        translation_placeholders={"device_id": device.id},
    )


def _days_of_week(vehicle: TeslaFleetVehicleData, days: list[str]) -> int | str:
    """Return selected days in the format the vehicle's command transport expects.

    Signed commands take Tesla's bitmask, the REST endpoint takes day names.
    """
    ordered = sorted(set(days), key=DAY_OF_WEEK_BITS.__getitem__)
    if vehicle.signing:
        return sum(DAY_OF_WEEK_BITS[day] for day in ordered)
    return ",".join(day.capitalize() for day in ordered)


def _minutes_after_midnight(value: time) -> int:
    """Convert a time of day to minutes after midnight."""
    return value.hour * 60 + value.minute


def _location(hass: HomeAssistant, call: ServiceCall) -> dict[str, float]:
    """Return the schedule location, defaulting to the Home Assistant location."""
    location: dict[str, float] = call.data.get(
        ATTR_LOCATION,
        {
            CONF_LATITUDE: hass.config.latitude,
            CONF_LONGITUDE: hass.config.longitude,
        },
    )
    return location


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up the Tesla Fleet services."""

    async def navigate_to_destination(call: ServiceCall) -> None:
        """Send an address, place name or map link to a vehicle."""
        vehicle = async_get_vehicle_for_service_call(hass, call)
        await wake_up_vehicle(vehicle)
        await handle_vehicle_command(
            vehicle.api.navigation_request(call.data[ATTR_DESTINATION])
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_NAVIGATE_TO_DESTINATION,
        navigate_to_destination,
        schema=probatio.Schema(
            {
                probatio.Required(CONF_DEVICE_ID): cv.string,
                probatio.Required(ATTR_DESTINATION): cv.string,
            }
        ),
    )

    async def navigate_to_coordinates(call: ServiceCall) -> None:
        """Send coordinates to a vehicle."""
        vehicle = async_get_vehicle_for_service_call(hass, call)
        await wake_up_vehicle(vehicle)
        await handle_vehicle_command(
            vehicle.api.navigation_gps_request(
                lat=call.data[ATTR_GPS][CONF_LATITUDE],
                lon=call.data[ATTR_GPS][CONF_LONGITUDE],
            )
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_NAVIGATE_TO_COORDINATES,
        navigate_to_coordinates,
        schema=probatio.Schema(
            {
                probatio.Required(CONF_DEVICE_ID): cv.string,
                probatio.Required(ATTR_GPS): {
                    probatio.Required(CONF_LATITUDE): cv.latitude,
                    probatio.Required(CONF_LONGITUDE): cv.longitude,
                },
            }
        ),
    )

    async def navigate_to_waypoints(call: ServiceCall) -> None:
        """Send an ordered trip using caller-supplied Google Place IDs."""
        vehicle = async_get_vehicle_for_service_call(hass, call)
        await wake_up_vehicle(vehicle)
        await handle_vehicle_command(
            vehicle.api.navigation_waypoints_request(
                ",".join(f"refId:{place_id}" for place_id in call.data[ATTR_PLACE_IDS])
            )
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_NAVIGATE_TO_WAYPOINTS,
        navigate_to_waypoints,
        schema=probatio.Schema(
            {
                probatio.Required(CONF_DEVICE_ID): cv.string,
                probatio.Required(ATTR_PLACE_IDS): probatio.All(
                    [probatio.All(str, probatio.Match(r"^[^\s,:]+\Z"))],
                    probatio.Length(min=1),
                ),
            }
        ),
    )

    async def get_schedules(call: ServiceCall) -> ServiceResponse:
        """Return the charge and precondition schedules of a vehicle."""
        vehicle = async_get_vehicle_for_service_call(hass, call, required_scopes=None)
        await wake_up_vehicle(vehicle)
        result = await handle_command(
            vehicle.api.vehicle_data(
                endpoints=[
                    VehicleDataEndpoint.CHARGE_SCHEDULE_DATA,
                    VehicleDataEndpoint.PRECONDITIONING_SCHEDULE_DATA,
                ]
            )
        )
        data = result.get("response")
        if not isinstance(data, dict):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="invalid_data"
            )
        charge_data: dict[str, Any] = data.get("charge_schedule_data") or {}
        precondition_data: dict[str, Any] = (
            data.get("preconditioning_schedule_data") or {}
        )
        return {
            "charge_schedules": charge_data.get("charge_schedules") or [],
            "precondition_schedules": (
                precondition_data.get("precondition_schedules") or []
            ),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_SCHEDULES,
        get_schedules,
        schema=probatio.Schema({probatio.Required(CONF_DEVICE_ID): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )

    async def add_charge_schedule(call: ServiceCall) -> None:
        """Add or modify a charge schedule on a vehicle."""
        vehicle = async_get_vehicle_for_service_call(
            hass, call, VEHICLE_CHARGING_CMDS_SCOPES
        )
        start_time: time | None = call.data.get(ATTR_START_TIME)
        end_time: time | None = call.data.get(ATTR_END_TIME)
        if start_time is None and end_time is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="missing_schedule_time"
            )
        location = _location(hass, call)
        await wake_up_vehicle(vehicle)
        await handle_vehicle_command(
            vehicle.api.add_charge_schedule(
                days_of_week=_days_of_week(vehicle, call.data[ATTR_DAYS_OF_WEEK]),
                enabled=call.data[ATTR_ENABLE],
                lat=location[CONF_LATITUDE],
                lon=location[CONF_LONGITUDE],
                start_time=(
                    None if start_time is None else _minutes_after_midnight(start_time)
                ),
                end_time=(
                    None if end_time is None else _minutes_after_midnight(end_time)
                ),
                one_time=call.data.get(ATTR_ONE_TIME),
                id=call.data.get(ATTR_ID),
                name=call.data.get(ATTR_NAME),
            )
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_CHARGE_SCHEDULE,
        add_charge_schedule,
        schema=probatio.Schema(
            {
                probatio.Required(CONF_DEVICE_ID): cv.string,
                probatio.Required(ATTR_DAYS_OF_WEEK): DAYS_OF_WEEK,
                probatio.Required(ATTR_ENABLE): cv.boolean,
                probatio.Optional(ATTR_LOCATION): LOCATION,
                probatio.Optional(ATTR_START_TIME): cv.time,
                probatio.Optional(ATTR_END_TIME): cv.time,
                probatio.Optional(ATTR_ONE_TIME): cv.boolean,
                probatio.Optional(ATTR_ID): SCHEDULE_ID,
                probatio.Optional(ATTR_NAME): cv.string,
            }
        ),
    )

    async def remove_charge_schedule(call: ServiceCall) -> None:
        """Remove a charge schedule from a vehicle."""
        vehicle = async_get_vehicle_for_service_call(
            hass, call, VEHICLE_CHARGING_CMDS_SCOPES
        )
        await wake_up_vehicle(vehicle)
        await handle_vehicle_command(
            vehicle.api.remove_charge_schedule(id=call.data[ATTR_ID])
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_CHARGE_SCHEDULE,
        remove_charge_schedule,
        schema=probatio.Schema(
            {
                probatio.Required(CONF_DEVICE_ID): cv.string,
                probatio.Required(ATTR_ID): SCHEDULE_ID,
            }
        ),
    )

    async def add_precondition_schedule(call: ServiceCall) -> None:
        """Add or modify a precondition schedule on a vehicle."""
        vehicle = async_get_vehicle_for_service_call(hass, call)
        location = _location(hass, call)
        await wake_up_vehicle(vehicle)
        await handle_vehicle_command(
            vehicle.api.add_precondition_schedule(
                days_of_week=_days_of_week(vehicle, call.data[ATTR_DAYS_OF_WEEK]),
                enabled=call.data[ATTR_ENABLE],
                lat=location[CONF_LATITUDE],
                lon=location[CONF_LONGITUDE],
                precondition_time=_minutes_after_midnight(
                    call.data[ATTR_PRECONDITION_TIME]
                ),
                id=call.data.get(ATTR_ID),
                one_time=call.data.get(ATTR_ONE_TIME),
                name=call.data.get(ATTR_NAME),
            )
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_PRECONDITION_SCHEDULE,
        add_precondition_schedule,
        schema=probatio.Schema(
            {
                probatio.Required(CONF_DEVICE_ID): cv.string,
                probatio.Required(ATTR_DAYS_OF_WEEK): DAYS_OF_WEEK,
                probatio.Required(ATTR_ENABLE): cv.boolean,
                probatio.Optional(ATTR_LOCATION): LOCATION,
                probatio.Required(ATTR_PRECONDITION_TIME): cv.time,
                probatio.Optional(ATTR_ID): SCHEDULE_ID,
                probatio.Optional(ATTR_ONE_TIME): cv.boolean,
                probatio.Optional(ATTR_NAME): cv.string,
            }
        ),
    )

    async def remove_precondition_schedule(call: ServiceCall) -> None:
        """Remove a precondition schedule from a vehicle."""
        vehicle = async_get_vehicle_for_service_call(hass, call)
        await wake_up_vehicle(vehicle)
        await handle_vehicle_command(
            vehicle.api.remove_precondition_schedule(id=call.data[ATTR_ID])
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_PRECONDITION_SCHEDULE,
        remove_precondition_schedule,
        schema=probatio.Schema(
            {
                probatio.Required(CONF_DEVICE_ID): cv.string,
                probatio.Required(ATTR_ID): SCHEDULE_ID,
            }
        ),
    )
