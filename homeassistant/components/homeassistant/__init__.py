"""Integration providing core pieces of infrastructure."""
import asyncio
import itertools as it
import logging

import voluptuous as vol

from homeassistant.auth.permissions.const import CAT_ENTITIES, POLICY_CONTROL
from homeassistant.components import persistent_notification
import homeassistant.config as conf_util
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    RESTART_EXIT_CODE,
    SERVICE_HOMEASSISTANT_RESTART,
    SERVICE_HOMEASSISTANT_STOP,
    SERVICE_RELOAD,
    SERVICE_SAVE_PERSISTENT_STATES,
    SERVICE_TOGGLE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
import homeassistant.core as ha
from homeassistant.exceptions import HomeAssistantError, Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv, recorder, restore_state
from homeassistant.helpers.entity_component import async_update_entity
from homeassistant.helpers.service import (
    async_extract_config_entry_ids,
    async_extract_referenced_entity_ids,
    async_register_admin_service,
)
from homeassistant.helpers.template import async_load_custom_templates
from homeassistant.helpers.typing import ConfigType

from .const import DATA_EXPOSED_ENTITIES, DOMAIN
from .exposed_entities import ExposedEntities

ATTR_ENTRY_ID = "entry_id"

_LOGGER = logging.getLogger(__name__)
SERVICE_RELOAD_CORE_CONFIG = "reload_core_config"
SERVICE_RELOAD_CONFIG_ENTRY = "reload_config_entry"
SERVICE_RELOAD_CUSTOM_TEMPLATES = "reload_custom_templates"
SERVICE_CHECK_CONFIG = "check_config"
SERVICE_UPDATE_ENTITY = "update_entity"
SERVICE_SET_LOCATION = "set_location"
SERVICE_RELOAD_ALL = "reload_all"
SCHEMA_UPDATE_ENTITY = vol.Schema({ATTR_ENTITY_ID: cv.entity_ids})
SCHEMA_RELOAD_CONFIG_ENTRY = vol.All(
    vol.Schema(
        {
            vol.Optional(ATTR_ENTRY_ID): str,
            **cv.ENTITY_SERVICE_FIELDS,
        },
    ),
    cv.has_at_least_one_key(ATTR_ENTRY_ID, *cv.ENTITY_SERVICE_FIELDS),
)


SHUTDOWN_SERVICES = (SERVICE_HOMEASSISTANT_STOP, SERVICE_HOMEASSISTANT_RESTART)

hass:ha.HomeAssistant = None

async def async_save_persistent_states(service: ha.ServiceCall) -> None:
        """Handle calls to homeassistant.save_persistent_states."""
        await restore_state.RestoreStateData.async_save_persistent_states(hass)

async def async_handle_turn_service(service: ha.ServiceCall) -> None:
    """Handle calls to homeassistant.turn_on/off."""
    referenced = async_extract_referenced_entity_ids(hass, service)
    all_referenced = referenced.referenced | referenced.indirectly_referenced

    # Generic turn on/off method requires entity id
    if not all_referenced:
        _LOGGER.error(
            "The service homeassistant.%s cannot be called without a target",
            service.service,
        )
        return

    # Group entity_ids by domain. groupby requires sorted data.
    by_domain = it.groupby(
        sorted(all_referenced), lambda item: ha.split_entity_id(item)[0]
    )

    tasks = []
    unsupported_entities = set()

    for domain, ent_ids in by_domain:
        # This leads to endless loop.
        if domain == DOMAIN:
            _LOGGER.warning(
                "Called service homeassistant.%s with invalid entities %s",
                service.service,
                ", ".join(ent_ids),
            )
            continue

        if not hass.services.has_service(domain, service.service):
            unsupported_entities.update(set(ent_ids) & referenced.referenced)
            continue

        # Create a new dict for this call
        data = dict(service.data)

        # ent_ids is a generator, convert it to a list.
        data[ATTR_ENTITY_ID] = list(ent_ids)

        tasks.append(
            hass.services.async_call(
                domain,
                service.service,
                data,
                blocking=True,
                context=service.context,
            )
        )

    if unsupported_entities:
        _LOGGER.warning(
            "The service homeassistant.%s does not support entities %s",
            service.service,
            ", ".join(sorted(unsupported_entities)),
        )

    if tasks:
        await asyncio.gather(*tasks)

async def async_handle_core_service(call: ha.ServiceCall) -> None:
    """Service handler for handling core services."""
    if call.service in SHUTDOWN_SERVICES and recorder.async_migration_in_progress(
        hass
    ):
        _LOGGER.error(
            "The system cannot %s while a database upgrade is in progress",
            call.service,
        )
        raise HomeAssistantError(
            f"The system cannot {call.service} "
            "while a database upgrade is in progress."
        )

    if call.service == SERVICE_HOMEASSISTANT_STOP:
        # Track trask in hass.data. No need to cleanup, we're stopping.
        hass.data["homeassistant_stop"] = asyncio.create_task(hass.async_stop())
        return

    errors = await conf_util.async_check_ha_config_file(hass)

    if errors:
        _LOGGER.error(
            "The system cannot %s because the configuration is not valid: %s",
            call.service,
            errors,
        )
        persistent_notification.async_create(
            hass,
            "Config error. See [the logs](/config/logs) for details.",
            "Config validating",
            f"{ha.DOMAIN}.check_config",
        )
        raise HomeAssistantError(
            f"The system cannot {call.service} "
            f"because the configuration is not valid: {errors}"
        )

    if call.service == SERVICE_HOMEASSISTANT_RESTART:
        # Track trask in hass.data. No need to cleanup, we're stopping.
        hass.data["homeassistant_stop"] = asyncio.create_task(
            hass.async_stop(RESTART_EXIT_CODE)
        )

async def async_handle_update_service(call: ha.ServiceCall) -> None:
    """Service handler for updating an entity."""
    if call.context.user_id:
        user = await hass.auth.async_get_user(call.context.user_id)

        if user is None:
            raise UnknownUser(
                context=call.context,
                permission=POLICY_CONTROL,
                user_id=call.context.user_id,
            )

        for entity in call.data[ATTR_ENTITY_ID]:
            if not user.permissions.check_entity(entity, POLICY_CONTROL):
                raise Unauthorized(
                    context=call.context,
                    permission=POLICY_CONTROL,
                    user_id=call.context.user_id,
                    perm_category=CAT_ENTITIES,
                )

    tasks = [
        async_update_entity(hass, entity) for entity in call.data[ATTR_ENTITY_ID]
    ]

    if tasks:
        await asyncio.gather(*tasks)

async def async_handle_reload_config(call: ha.ServiceCall) -> None:
    """Service handler for reloading core config."""
    try:
        conf = await conf_util.async_hass_config_yaml(hass)
    except HomeAssistantError as err:
        _LOGGER.error(err)
        return

    # auth only processed during startup
    await conf_util.async_process_ha_core_config(hass, conf.get(ha.DOMAIN) or {})

async def async_set_location(call: ha.ServiceCall) -> None:
    """Service handler to set location."""
    await hass.config.async_update(
        latitude=call.data[ATTR_LATITUDE], longitude=call.data[ATTR_LONGITUDE]
    )

async def async_handle_reload_templates(call: ha.ServiceCall) -> None:
    """Service handler to reload custom Jinja."""
    await async_load_custom_templates(hass)

async def async_handle_reload_config_entry(call: ha.ServiceCall) -> None:
    """Service handler for reloading a config entry."""
    reload_entries = set()
    if ATTR_ENTRY_ID in call.data:
        reload_entries.add(call.data[ATTR_ENTRY_ID])
    reload_entries.update(await async_extract_config_entry_ids(hass, call))
    if not reload_entries:
        raise ValueError("There were no matching config entries to reload")
    await asyncio.gather(
        *(
            hass.config_entries.async_reload(config_entry_id)
            for config_entry_id in reload_entries
        )
    )

async def async_handle_reload_all(call: ha.ServiceCall) -> None:
    """Service handler for calling all integration reload services.

    Calls all reload services on all active domains, which triggers the
    reload of YAML configurations for the domain that support it.

    Additionally, it also calls the `homeasssitant.reload_core_config`
    service, as that reloads the core YAML configuration, the
    `frontend.reload_themes` service that reloads the themes, and the
    `homeassistant.reload_custom_templates` service that reloads any custom
    jinja into memory.

    We only do so, if there are no configuration errors.
    """

    if errors := await conf_util.async_check_ha_config_file(hass):
        _LOGGER.error(
            "The system cannot reload because the configuration is not valid: %s",
            errors,
        )
        raise HomeAssistantError(
            "Cannot quick reload all YAML configurations because the "
            f"configuration is not valid: {errors}"
        )

    services = hass.services.async_services()
    tasks = [
        hass.services.async_call(
            domain, SERVICE_RELOAD, context=call.context, blocking=True
        )
        for domain, domain_services in services.items()
        if domain != "notify" and SERVICE_RELOAD in domain_services
    ] + [
        hass.services.async_call(
            domain, service, context=call.context, blocking=True
        )
        for domain, service in (
            (ha.DOMAIN, SERVICE_RELOAD_CORE_CONFIG),
            ("frontend", "reload_themes"),
            (ha.DOMAIN, SERVICE_RELOAD_CUSTOM_TEMPLATES),
        )
    ]

    await asyncio.gather(*tasks)


async def register_all_service():
    admin_services = [
        (SERVICE_RELOAD_CORE_CONFIG, async_handle_reload_config, None),
        (SERVICE_HOMEASSISTANT_STOP, async_handle_core_service, None),
        (SERVICE_HOMEASSISTANT_RESTART, async_handle_core_service, None),
        (SERVICE_CHECK_CONFIG, async_handle_core_service, None),
        (SERVICE_SET_LOCATION, async_set_location, 
         vol.Schema({ATTR_LATITUDE: cv.latitude, ATTR_LONGITUDE: cv.longitude})),
        (SERVICE_RELOAD_CUSTOM_TEMPLATES, async_handle_reload_templates, None),
        (SERVICE_RELOAD_CONFIG_ENTRY, async_handle_reload_config_entry, 
         SCHEMA_RELOAD_CONFIG_ENTRY),
        (SERVICE_RELOAD_ALL, async_handle_reload_all, None),
    ]
    for service, handler, schema in admin_services:
        async_register_admin_service(hass, ha.DOMAIN, service, handler, schema=schema)

    service_schema = vol.Schema({ATTR_ENTITY_ID: cv.entity_ids}, extra=vol.ALLOW_EXTRA)
    async_services = [
        (SERVICE_SAVE_PERSISTENT_STATES, async_save_persistent_states, None),
        (SERVICE_TURN_OFF, async_handle_turn_service, service_schema),
        (SERVICE_TURN_ON, async_handle_turn_service, service_schema),
        (SERVICE_TOGGLE, async_handle_turn_service, service_schema),
        (SERVICE_UPDATE_ENTITY, async_handle_update_service, SCHEMA_UPDATE_ENTITY),
    ]
    for service, handler, schema in async_services:
        hass.services.async_register(hass, ha.DOMAIN, service, handler, schema=schema)


async def async_setup(local_hass: ha.HomeAssistant, _) -> bool:
    """Set up general services related to Home Assistant."""
    global hass
    hass = local_hass
    register_all_service()
    exposed_entities = ExposedEntities(hass)
    await exposed_entities.async_initialize()
    hass.data[DATA_EXPOSED_ENTITIES] = exposed_entities

    return True
