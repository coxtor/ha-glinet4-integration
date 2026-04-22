"""Support for turning on and off Pi-hole system."""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING, Any

from gli4py.error_handling import APIClientError, NonZeroResponse


def _rpc_failed(result: Any) -> str | None:
    """Return an error message when the router returned an RPC-level error."""
    if isinstance(result, dict) and result.get("err_code"):
        return f"err_code={result.get('err_code')} err_msg={result.get('err_msg')!r}"
    return None

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .router import wifi_iface_band_label

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .router import GLinetRouter, WifiInterface, WireGuardClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    _: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Pi-hole switch."""
    router: GLinetRouter = entry.runtime_data
    switches: list[
        WifiApSwitch | WireGuardSwitch | TailscaleSwitch | VpnToggleSwitch
    ] = []
    if router.wireguard_clients:
        # TODO detect all configured wireguard, openvpn, shadowsocks and
        # TOR clients & servers with router/vpn/status? and gen a switch for each
        switches = [
            WireGuardSwitch(router, client)
            for client in router.wireguard_clients.values()
        ]
        switches.append(VpnToggleSwitch(router))
    if router.tailscale_switch_exposed:
        switches.append(TailscaleSwitch(router))
    for iface_name, iface in router.wifi_ifaces.items():
        switches.append(WifiApSwitch(router, iface_name, iface))
    if switches:
        async_add_entities(switches, True)


class GliSwitchBase(SwitchEntity):
    """GL-inet switch base class."""

    def __init__(self, router: GLinetRouter) -> None:
        """Initialize a GLinet device."""
        self._router = router
        self._attr_device_info = router.device_info
        self._attr_is_on: bool | None

    _attr_has_entity_name = True
    _attr_should_poll = False

    @property
    def is_on(self) -> bool | None:
        """Return if the service is on."""
        return self._attr_is_on

    @property
    def entity_category(self) -> EntityCategory:
        """A config entity."""
        return EntityCategory.CONFIG


class WifiApSwitch(GliSwitchBase):
    """A WiFi AccessPoint switch."""

    def __init__(
        self, router: GLinetRouter, iface_name: str, iface: WifiInterface
    ) -> None:
        """Initialize a GLinet device."""
        super().__init__(router)
        self._iface_name = iface_name
        self._iface = iface

    @property
    def icon(self) -> str:
        """Return AP state icon."""
        if self.is_on:
            return "mdi:wifi"
        return "mdi:wifi-off"

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        base = self._iface.ssid if self._iface.ssid else self._iface.name
        band = wifi_iface_band_label(self._iface_name)
        if band:
            return f"{base} ({band})"
        return base

    @property
    def unique_id(self) -> str:
        """Return the unique id of the switch."""
        return f"glinet_switch/{self._router.factory_mac}/iface_{self._iface_name}"

    @property
    def extra_state_attributes(self) -> dict[str, str | bool]:
        """Return the attributes."""
        attrs: dict[str, str | bool] = {}
        attrs["interface"] = self._iface.name
        attrs["guest"] = self._iface.guest
        attrs["ssid"] = self._iface.ssid
        attrs["hidden"] = self._iface.hidden
        attrs["encryption"] = self._iface.encryption
        if band := wifi_iface_band_label(self._iface_name):
            attrs["band"] = band
        return attrs

    async def async_turn_on(self, **_: Any) -> None:
        """Turn on the AP."""
        try:
            _LOGGER.debug("Enabling WiFi interface %s", self._iface_name)
            await self._router.api.wifi_iface_set_enabled(self._iface_name, True)
        except OSError:
            _LOGGER.exception(
                "Unable to enable WiFi interface %s",
                self._iface_name,
            )
        else:
            # be optimistic
            self._attr_is_on = True
            self.async_write_ha_state()

            # fetch the state #TODO try block?
            await self._router.update_wifi_ifaces_state()
            await self.async_update()

    async def async_turn_off(self, **_: Any) -> None:
        """Turn off the AP."""
        try:
            _LOGGER.debug("Disabling WiFi interface %s", self._iface_name)
            await self._router.api.wifi_iface_set_enabled(self._iface_name, False)
        except OSError:
            _LOGGER.exception(
                "Unable to disable WiFi interface %s",
                self._iface_name,
            )
        else:
            # be optimistic
            self._attr_is_on = False
            self.async_write_ha_state()

            # fetch the state #TODO try block?
            await self._router.update_wifi_ifaces_state()
            await self.async_update()

    @callback
    async def async_update(self) -> None:
        """Update the switch state."""
        _LOGGER.debug(
            "Updating WiFi AP switch with stored state for %s",
            self._iface_name,
        )
        self._iface = self._router.wifi_ifaces.get(self._iface_name) or self._iface
        self._attr_is_on = self._iface.enabled


class TailscaleSwitch(GliSwitchBase):
    """A tailscale switch."""

    _attr_icon = "mdi:vpn"  # TODO would be better to have MDI style icons for each of the VPN types

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        # TODO we could add the login_name here, but we lose access to that value when the connection drops
        return "Tailscale"

    @property
    def unique_id(self) -> str:
        """Return the unique id of the switch."""
        return f"glinet_switch/{self._router.factory_mac}/tailscale"

    async def async_turn_on(self, **_: Any) -> None:
        """Turn on the service."""
        try:
            _LOGGER.debug("Enabling tailscale")
            await self._router.api.tailscale_start()
            # TODO since the state takes a while to change we may
        except OSError:
            _LOGGER.exception("Unable to enable tailscale connection")
        else:
            self._attr_is_on = True
            self.async_write_ha_state()

    async def async_turn_off(self, **_: Any) -> None:
        """Turn off the service."""
        try:
            _LOGGER.debug("Enabling tailscale")
            await self._router.api.tailscale_stop()
        except OSError:
            _LOGGER.exception("Unable to stop tailscale connection")
        else:
            self._attr_is_on = False
            self.async_write_ha_state()

    @property
    def lan_access(self) -> bool | None:
        """Whether the router exposes the LAN as a subnet."""
        la = self._router.tailscale_config.get("lan_enabled")
        if la is not None:
            return bool(la)
        return None

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Enabled by default."""
        return self._router.tailscale_switch_exposed

    @property
    def entity_registry_visible_default(self) -> bool:
        """Enabled by default."""
        return self._router.tailscale_switch_exposed

    @callback
    async def async_update(self) -> None:
        """Update the switch state from cached router data."""
        _LOGGER.debug("Updating Tailscale switch state from stored info")
        self._attr_is_on = self._router.tailscale_connection


class WireGuardSwitch(GliSwitchBase):
    """Representation of a VPN switch."""

    # TODO make class, client/server/VPN type agnostic and appreciate >1 can be configured of each
    # And also appreciates that some combinations of states are not permitted by Gl-inet
    # such as can't have a server and a client active of the same VPN type, also can't have
    # multiples of any one type etc etc
    def __init__(self, router: GLinetRouter, client: WireGuardClient) -> None:
        """Initialize a GLinet device."""
        super().__init__(router)
        self._client = client
        self._attr_device_info = router.device_info
        self._attr_is_on: bool = False

    _attr_icon = "mdi:vpn"  # TODO would be better to have MDI style icons for each of the VPN types

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return f"WG Client {self._client.name}"

    @property
    def unique_id(self) -> str:
        """Return the unique id of the switch."""
        return f"glinet_switch/{self._router.factory_mac}/{self._client.name}/wireguard_client"

    async def async_turn_on(self, **_: Any) -> None:
        """Turn on the service."""
        if self._client.tunnel_id is None:
            _LOGGER.warning(
                "WG switch: tunnel_id unknown for %s, refreshing state first",
                self._client.name,
            )
            await self._router.update_wireguard_client_state()
        peer_or_tunnel = self._client.tunnel_id or self._client.peer_id
        _LOGGER.warning(
            "WG switch turn_on name=%s group_id=%s peer_id=%s tunnel_id=%s (arg=%s)",
            self._client.name,
            self._client.group_id,
            self._client.peer_id,
            self._client.tunnel_id,
            peer_or_tunnel,
        )
        try:
            # TODO Verify that the API doesn't do this for us
            if (
                self._client.tunnel_id
                is None  # This confirms we are using older firmware
                and self._router.connected_wireguard_clients is not None
                and self._client not in self._router.connected_wireguard_clients
            ):
                for client in self._router.connected_wireguard_clients:
                    _LOGGER.warning(
                        "WG switch pre-stopping active client %s", client.name
                    )
                    await self._router.api.wireguard_client_stop(client.peer_id)
                # TODO may need to introduce a delay here, or await confirmation of the stop

            result = await self._router.api.wireguard_client_start(
                self._client.group_id, peer_or_tunnel
            )
        except (OSError, APIClientError, NonZeroResponse) as err:
            _LOGGER.exception("Unable to enable WG client: %s", err)
            return
        _LOGGER.warning("wireguard_client_start returned: %r", result)
        if (rpc_err := _rpc_failed(result)) is not None:
            _LOGGER.warning(
                "Router rejected WG start for %s: %s. On firmware >= 4.8 "
                "only the peer assigned to the active tunnel slot can be "
                "started; switching peers requires an RPC that is not yet "
                "implemented.",
                self._client.name,
                rpc_err,
            )
            return
        # Optimistic; next router poll will confirm. Don't refresh now:
        # the tunnel takes a few seconds to come up and the signal would
        # flip us back to off immediately.
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **_: Any) -> None:
        """Turn off the service."""
        try:
            await self._router.api.wireguard_client_stop(
                self._client.tunnel_id or self._client.peer_id
            )
            # TODO may need to introduce a delay here, or await confirmation of the stop
        except OSError:
            _LOGGER.exception("Unable to stop WG client")
        else:
            # be optimistic
            self._attr_is_on = False
            self.async_write_ha_state()
            await self._router.update_wireguard_client_state()
            await self.async_update()

    async def async_added_to_hass(self) -> None:
        """Subscribe to VPN state changes so sibling toggles re-render."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                self._router.signal_vpn_update,
                self._handle_vpn_update,
            )
        )

    @callback
    def _handle_vpn_update(self) -> None:
        """Re-read cached router state and push to HA."""
        self._attr_is_on = self._client in (self._router.wireguard_connections or [])
        self.async_write_ha_state()

    @callback
    async def async_update(self) -> None:
        """Update the switch state. A user may have many so don't call the API for each."""
        _LOGGER.debug("Updating WG client switch state from stored info")
        self._attr_is_on = self._client in (self._router.wireguard_connections or [])


class VpnToggleSwitch(GliSwitchBase):
    """A simple VPN on/off toggle that picks a WG client when turning on."""

    _attr_icon = "mdi:vpn"

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return "VPN"

    @property
    def unique_id(self) -> str:
        """Return the unique id of the switch."""
        return f"glinet_switch/{self._router.factory_mac}/vpn_toggle"

    async def async_added_to_hass(self) -> None:
        """Subscribe to VPN state changes from the router."""
        self._attr_is_on = bool(self._router.wireguard_connections)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                self._router.signal_vpn_update,
                self._handle_vpn_update,
            )
        )

    @callback
    def _handle_vpn_update(self) -> None:
        """Re-read cached router state and push to HA."""
        self._attr_is_on = bool(self._router.wireguard_connections)
        self.async_write_ha_state()

    def _pick_random_peer(self) -> WireGuardClient | None:
        """Pick a random peer from all configured clients."""
        clients = list(self._router.wireguard_clients.values())
        if not clients:
            return None
        return random.choice(clients)

    def _active_tunnel_id(self) -> int | None:
        """Return the tunnel_id of the currently-provisioned tunnel slot (4.8+)."""
        for c in self._router.wireguard_clients.values():
            if c.tunnel_id is not None:
                return c.tunnel_id
        return None

    async def _assign_peer_and_start(
        self, peer: WireGuardClient, tunnel_id: int
    ) -> dict | None:
        """Reassign the tunnel slot to a new peer and enable it.

        gli4py has no wrapper for this, so we call the raw RPC. Endpoint is
        based on community research — if the router rejects it, we log and
        let the caller fall back to plain enable of whatever peer is active.
        """
        payload = self._router.api.gen_sid_payload(
            "call",
            [
                "vpn-client",
                "set_tunnel",
                {
                    "enabled": True,
                    "tunnel_id": tunnel_id,
                    "via": {
                        "group_id": peer.group_id,
                        "peer_id": peer.peer_id,
                        "type": "wireguard",
                    },
                },
            ],
            self._router.api.sid,
        )
        _LOGGER.warning(
            "VPN toggle: raw set_tunnel to switch peer name=%s peer_id=%s tunnel_id=%s",
            peer.name,
            peer.peer_id,
            tunnel_id,
        )
        try:
            return await self._router.api._request(payload)  # noqa: SLF001
        except (OSError, APIClientError, NonZeroResponse) as err:
            _LOGGER.exception("Raw set_tunnel failed: %s", err)
            return None

    async def async_turn_on(self, **_: Any) -> None:
        """Enable VPN; pick a random peer and switch the tunnel slot to it."""
        if self._router.wireguard_connections:
            _LOGGER.debug("VPN already connected, turn_on is a no-op")
            self._attr_is_on = True
            self.async_write_ha_state()
            return

        peer = self._pick_random_peer()
        if peer is None:
            _LOGGER.warning("No WireGuard clients configured, cannot start VPN")
            return

        tunnel_id = peer.tunnel_id or self._active_tunnel_id()
        if tunnel_id is None:
            _LOGGER.warning(
                "VPN toggle: no tunnel_id known yet, refreshing router state"
            )
            await self._router.update_wireguard_client_state()
            tunnel_id = peer.tunnel_id or self._active_tunnel_id()

        _LOGGER.warning(
            "VPN toggle picked random peer=%s peer_id=%s group_id=%s tunnel_id=%s",
            peer.name,
            peer.peer_id,
            peer.group_id,
            tunnel_id,
        )

        if tunnel_id is not None and peer.tunnel_id != tunnel_id:
            # Peer isn't the one in the slot → reassign via raw RPC.
            result = await self._assign_peer_and_start(peer, tunnel_id)
            _LOGGER.warning("raw set_tunnel returned: %r", result)
            if result is None or _rpc_failed(result) is not None:
                _LOGGER.warning(
                    "Peer switch RPC not accepted; falling back to enabling "
                    "whichever peer is currently in the tunnel slot"
                )
                # Fall back to just enabling the active-slot peer.
                peer = next(
                    (
                        c
                        for c in self._router.wireguard_clients.values()
                        if c.tunnel_id is not None
                    ),
                    peer,
                )
            else:
                # set_tunnel with enabled=true already started it; update
                # local state and we're done.
                self._attr_is_on = True
                self.async_write_ha_state()
                return

        # Plain enable path (peer already assigned, or fallback).
        call_arg = peer.tunnel_id or peer.peer_id
        _LOGGER.warning(
            "VPN toggle starting peer=%s group_id=%s tunnel_id=%s (call arg=%s)",
            peer.name,
            peer.group_id,
            peer.tunnel_id,
            call_arg,
        )
        try:
            result = await self._router.api.wireguard_client_start(
                peer.group_id, call_arg
            )
        except (OSError, APIClientError, NonZeroResponse) as err:
            _LOGGER.exception(
                "Unable to start VPN (peer %s): %s", peer.name, err
            )
            return
        _LOGGER.warning("wireguard_client_start returned: %r", result)
        if (rpc_err := _rpc_failed(result)) is not None:
            _LOGGER.warning(
                "Router rejected VPN start for %s: %s", peer.name, rpc_err
            )
            return

        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **_: Any) -> None:
        """Disconnect any active VPN clients (also flips individual WG switches)."""
        active = list(self._router.wireguard_connections or [])
        if not active:
            self._attr_is_on = False
            self.async_write_ha_state()
            return

        for client in active:
            peer_or_tunnel = client.tunnel_id or client.peer_id
            _LOGGER.warning(
                "VPN toggle stopping WG client name=%s (arg=%s)",
                client.name,
                peer_or_tunnel,
            )
            try:
                result = await self._router.api.wireguard_client_stop(peer_or_tunnel)
            except (OSError, APIClientError, NonZeroResponse) as err:
                _LOGGER.exception(
                    "Unable to stop VPN (WG client %s): %s", client.name, err
                )
                continue
            _LOGGER.warning("wireguard_client_stop returned: %r", result)

        self._attr_is_on = False
        self.async_write_ha_state()
        await self._router.update_wireguard_client_state()

    @callback
    async def async_update(self) -> None:
        """Update toggle state from cached router info."""
        self._attr_is_on = bool(self._router.wireguard_connections)
