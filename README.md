# Bermuda BLE Trilateration: BenCodez measurement toolkit fork

This custom fork adds measurement, independently labelled calibration and diagnostics to [Bermuda by Ashley Gittins](https://github.com/agittins/bermuda), with the 18 fixes inherited from [davidcoulson/bermuda-patched](https://github.com/davidcoulson/bermuda-patched). It is not an official upstream release. The integration domain remains `bermuda`; existing devices, entity identities and settings remain compatible, and legacy tracking stays the default.

The toolkit no longer adds a sidebar item. Administrators can open `/bermuda-toolkit` on their Home Assistant server when needed. Radio calibration appears first; optional tuning is under **Advanced**, and descriptive fields are under **Hardware details**. Read the [measurement toolkit guide](docs/measurement-toolkit.md) for controls, measured calibration, defaults, limits and privacy, or the [panel reference](docs/toolkit-panel.md) for a shorter walkthrough.

## Twelve additions

1. **Stable measurement API:** source IDs/stamps, raw RSSI, raw/filtered range, quality and effective models without hundreds of distance entities.
2. **Reliable scanner matching:** explicit Bluetooth/config-entry bindings, preserved scanner identities and duplicate-name calibration selectors.
3. **Scanner health:** packet freshness, cadence, last-valid data and structured rejection reasons.
4. **Live corrections:** calibration preview, explicit apply and saved applicable rollback.
5. **Guided measured calibration:** slant distances in metres, independent locations/beacons and held-out validation.
6. **Safe automatic calibration:** opt-in independent evidence, capped steps, cooldown and regression rollback.
7. **Receiver/beacon profiles:** hardware notes and explicit radio parameters, with inherited defaults.
8. **Receiver reliability and radius limits:** optional election weights and local limits while preserving distant raw observations.
9. **Per-device filtering:** optional pet/stationary policies that reject spikes and accept sustained movement; legacy remains default.
10. **Bounded history, replay and shadow:** locally saved labelled snapshots, redacted exports and explicit validated promotion.
11. **Supervised room fingerprints:** independent user labels, unknown/ambiguity rejection, advisory Suggested Area and optional guarded live selection.
12. **Runtime performance diagnostics:** completed update timing, observation counts, bounded workers and skipped background cycles.

Automatic calibration, reliability weighting, fingerprint live area selection and continuous recording are **off by default**. Hardware descriptions and antenna ratings do not create invented calibration corrections. Explicit measured captures and configured fixed references can save bounded local evidence while continuous recording is off. Range error measures ranging, not map position accuracy.

## Install this fork with HACS

Use the custom repository **`https://github.com/BenCodez/bermuda`**, category **Integration**:

[![Open this fork in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=BenCodez&repository=bermuda&category=Integration)

For a new installation, add that repository in **HACS → Custom repositories**, download its Bermuda package, restart Home Assistant and add **Bermuda BLE Trilateration** under **Settings → Devices & services**.

Normal HACS downloads require a published release from this fork with its `bermuda.zip` asset. While the feature PR is unmerged or unreleased, its branch source ZIP can be used for the manual replacement below.

To replace upstream or bermuda-patched while preserving your installation:

1. **Keep the existing Bermuda integration entries under Settings → Devices & services**; do not delete their devices/entities/settings or add a second Bermuda entry.
2. In HACS, remove the previously downloaded Bermuda package and install this fork's release before restarting. If HACS displays **Integration is configured**, use **Ignore** when offered to retain the Home Assistant configuration. If HACS requires deleting the integration and offers no **Ignore**, use the manual replacement below. Add the BenCodez custom repository above and check the repository owner when similarly named entries appear.
3. Restart Home Assistant. The existing `bermuda` entries load the fork and keep their established entity identities and option keys.

**Manual replacement:** Keep the Home Assistant entries and `.storage` intact. Replace only `config/custom_components/bermuda` with this fork's component files. From a branch source ZIP, copy its `custom_components/bermuda` directory. A release's `bermuda.zip` contains the component files at the archive root; extract them directly into `config/custom_components/bermuda`, without another nested `bermuda` folder. Complete the replacement before restarting Home Assistant. If the old HACS repository remains registered, an update from it can overwrite these files; switch HACS to this fork's repository for future updates.

The HACS repository/package selection changes; Home Assistant's integration configuration entries stay in place. Only one downloaded implementation can occupy `custom_components/bermuda`, so install this as a replacement, not a parallel integration. To return to upstream, replace only the HACS package with the upstream package and restart, retaining the Home Assistant integration entries. Existing legacy options remain in their original namespace.

### Release update notifications

HACS provides the Home Assistant update entity for this fork. Register and download
**BenCodez/bermuda**, select a numbered release rather than `main`, and restart Home
Assistant. Releases appear after HACS refreshes its repository information; use
the repository's **Update information** action to check sooner.

If HACS shows a newer release but Home Assistant's **Settings → System → Updates**
does not, check **Settings → Devices & services → Entities**, filter by **HACS** and
the `update` domain, and enable this repository's update entity if disabled. A
manual file replacement does not register the fork with HACS or create its update
entity. Keep your existing Bermuda integration configuration when switching packages.

For maintainers: publish a new, higher numeric version tag such as `v1.0.3`,
targeting the commit containing the release workflow. Wait for the **Release**
action to attach `bermuda.zip`; the ZIP's manifest and runtime versions are stamped
from the tag and verified. The component domain remains `bermuda`, and the ZIP
continues to be signed. Draft releases do not trigger packaging. Prereleases such
as `v1.0.3-beta.1` require beta releases enabled in HACS; use a normal release for
standard update notifications. Publishing a release does not push an immediate
notification to Home Assistant.

## Patched baseline: 18 inherited fixes

| Fix | Upstream PR |
|---|---|
| Metadevices now copy their source's name, manufacturer and beacon fields (the copy loop was a no-op) | [#832](https://github.com/agittins/bermuda/pull/832) |
| A duplicate entry in the prune list no longer raises `KeyError` and aborts the update cycle | [#833](https://github.com/agittins/bermuda/pull/833) |
| One scanner that cannot be resolved no longer stops entity creation for every other scanner, and a scanner that comes up after startup gets its distance sensors without a reload | [#835](https://github.com/agittins/bermuda/pull/835) |
| IRKs are no longer written in full to diagnostics or the debug log | [#840](https://github.com/agittins/bermuda/pull/840) |
| `bermuda.dump_devices` requires an admin when the call carries a user | [#842](https://github.com/agittins/bermuda/pull/842) |
| Changing a proxy's area takes effect without a reload (and the "no AREA" repair clears) | [#857](https://github.com/agittins/bermuda/pull/857) |
| A proxy is matched to its own registry entry, not a neighbour's with an adjacent MAC (#859) | [#860](https://github.com/agittins/bermuda/pull/860) |
| A proxy's own entry beats a router integration's entry for the same MAC (wrong name, "no AREA" repair, #722) | [#862](https://github.com/agittins/bermuda/pull/862) |
| Random BLE addresses are classified correctly (a bitwise `&` where `==` was meant left random-static ones mislabelled) | [#863](https://github.com/agittins/bermuda/pull/863) |
| Device pruning no longer raises `IndexError` when too few devices can be pruned, and prunes the right number | [#848](https://github.com/agittins/bermuda/pull/848) (by @barneyonline) |
| Scanners are found with Home Assistant's supported registry lookup (the old one is removed in 2027.9) | [#847](https://github.com/agittins/bermuda/pull/847) (by @barneyonline) |
| Scanner lists in the options and calibration screens are sorted by name | [#836](https://github.com/agittins/bermuda/pull/836) (by @BrawnyBravo) |
| An attenuation of 0 (or NaN) in the options no longer aborts every update cycle; both distance settings are validated | [#864](https://github.com/agittins/bermuda/pull/864) |
| A ref_power change shows the new distances at once (the cache-bypass check could never pass) | [#865](https://github.com/agittins/bermuda/pull/865) |
| A truncated iBeacon advert no longer creates a bogus `None_None_None` beacon | [#866](https://github.com/agittins/bermuda/pull/866) |
| iBeacons nobody tracks are pruned once they go stale, instead of piling up forever | [#867](https://github.com/agittins/bermuda/pull/867) |
| Deleting a tracked device stops tracking it (iBeacon ids were split at an underscore, and the device list re-enabled it every cycle) | [#868](https://github.com/agittins/bermuda/pull/868) |
| Fewer recorder rows: Distance is republished only when it moves by max(0.2 m, 10%), and Floor no longer carries the area attributes | [#869](https://github.com/agittins/bermuda/pull/869) |

The fixes above come from the patched baseline and credited upstream pull requests. They are separate from this fork's twelve toolkit additions; their inclusion does not imply an official upstream release. Report fork-specific problems in the relevant [BenCodez/bermuda pull-request discussion](https://github.com/BenCodez/bermuda/pulls); this fork currently has repository issues disabled.

The following general Bermuda documentation and artwork are retained from upstream. The upstream wiki explains shared tracking setup; the [measurement toolkit guide](docs/measurement-toolkit.md) describes this fork's additions.

---

![Bermuda Logo](img/logo@2x.png)

[![Open this fork in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=BenCodez&repository=bermuda&category=Integration)

# Bermuda BLE Trilateration

- Track bluetooth devices by Area (Room) in [Home Assistant](https://home-assistant.io/), using [ESPHome](https://esphome.io/) [Bluetooth Proxies](https://esphome.io/components/bluetooth_proxy.html) and Shelly Gen2 or later devices.

- (eventually) Triangulate device positions! Like, on a map. Maybe.


[![Upstream Bermuda Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)
[![HomeAssistant Minimum Version][haminverbadge]][haminver]
[![pre-commit][pre-commit-shield]][pre-commit]
[![Black][black-shield]][black]
[![hacs][hacsbadge]][hacs]
[![Upstream Project Maintenance][maintenance-shield]][user_profile]
[![Discord][discord-shield]][discord]
[![Community Forum][forum-shield]][forum]

[![GitHub Sponsors][sponsorsbadge]][sponsors]
[![BuyMeCoffee][buymecoffeebadge]][buymecoffee]
[![Patreon Sponsorship][patreonbadge]][patreon]


## What it does:

Bermuda aims to let you track any bluetooth device, and have Home Assistant tell you where in your house that device is. The only extra hardware you need are esp32 devices running esphome that act as bluetooth proxies. Alternatively, Shelly Plus devices can also perform this function.

- Area-based device location (ie, device-level room prescence) is working reasonably well.
- Creates sensors for Area and Distance for devices you choose
- Supports iBeacon devices, including those with randomised MAC addresses (like Android phones running HA Companion App)
- Supports IRK (resolvable keys) via the [Private BLE Device](https://www.home-assistant.io/integrations/private_ble_device/) core component. Once your iOS device (or Android!) is set up in Private BLE Device, it will automatically receive Bermuda sensors as well!
- Creates `device_tracker` entities for chosen devices, which can be linked to "Person"s for Home/Not Home tracking
- Configurable settings for rssi reference level, environmental attenuation, max tracking radius
- Provides a comprehensive json/yaml dump of devices and their distances from each bluetooth
  receiver, via the `bermuda.dump_devices` service.

## What you need:

- Home Assistant. The current release of Bermuda requires at least ![haminverbadge]
- One or more devices providing bluetooth proxy information to HA using HA's bluetooth backend. These can be:
  - ESPHome devices with the `bluetooth_proxy` component enabled. I like the D1-Mini32 boards because they're cheap and easy to deploy.
  - Shelly Plus or later devices with Bluetooth proxying enabled in the Shelly integration.
  - USB Bluetooth on your HA host. This is not ideal, since they do not timestamp the advertisement packets and finding a well-supported usb bluetooth adaptor is non-trivial. However they can be used for simple "Home/Not Home" tracking, and basic Area distance support is enabled currently.

- Some bluetooth BLE devices you want to track. Phones, smart watches, beacon tiles, thermometers etc.

- Bermuda! I strongly recommend installing Bermuda via HACS:
  [![Open this fork in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=BenCodez&repository=bermuda&category=Integration)

## Documentation and help

[The Wiki](https://github.com/agittins/bermuda/wiki/) is the primary and official source of information for setting up Bermuda.

[Discussions](https://github.com/agittins/bermuda/discussions/) contain both official and user-contributed guides, how-tos and general Q&A.

[HA Community Thread for Bermuda](https://community.home-assistant.io/t/bermuda-bluetooth-ble-room-presence-and-tracking-custom-integration/625780/1) contains a *wealth* of information from and for users of Bermuda, and is where many folk first ask for assistance in setting up.

## Screenshots

After installing, the integration should be visible in Settings, Devices & Services

![The integration, in Settings, Devices & Services](img/screenshots/integration.png)

Press the `CONFIGURE` button to see the configuration dialog. At the bottom is a field
where you can enter/list any bluetooth devices the system can see. Choosing devices
will add them to the configured devices list and creating sensor entities for them. See [How Do The Settings Work?](#how-do-the-settings-work) for more info.

![Bermuda integration configuration option flow](img/screenshots/configuration.png)

Choosing the device screen shows the current sensors and other info. Note that there are extra sensors in the "not shown" section that are disabled by default (the screenshot shows several of these enabled already). You can edit the properties of these to enable them for more detailed data on your device locations. This is primarily intended for troubleshooting or development, though.

![Screenshot of device information view](img/screenshots/deviceinfo.png)

The sensor information also includes attributes area name and id, relevant MAC addresses
etc.

![Bermuda sensor information](img/screenshots/sensor-info.png)

In Settings, People, you can define any Bermuda device to track home/away status
for any person/user.

![Assign a Bermuda sensor for Person tracking](img/screenshots/person-tracker.png)

## FAQ

See [The FAQ](https://github.com/agittins/bermuda/wiki/FAQ) in the Wiki!

## Hacking tips

Wanna improve this? Awesome! Bear in mind this is my first ever HA
integration, and I'm much more greybeard sysadmin than programmer, so ~~if~~where
I'm doing stupid things I really would welcome some improvements!

You can start by using the service `bermuda.dump_devices` to examine the
internal state.

### Using `bermuda.dump_devices` service

Just calling the service `bermuda.dump_devices` will give you a full dump of the internal
data structures that bermuda uses to track and calculate its state. This can be helpful
for working out what's going on and troubleshooting, or to use if you have a very custom
need that you can solve with template sensors etc.

If called with no parameters, the service will return all data. parameters are available
which let you limit or reformat the resulting data to make it easier to work with. In particular
the `addresses` parameter is helpful to only return data relevant for one or more MAC addresses
(or iBeacon UUIDs).
See the information on parameters in the `Services` page in Home Assistant, under `Developer Tools`.

Important: If you decide to use the results of this call for your own templates etc, bear in mind that
the format might change in any release, and won't necessarily be considered a "breaking change".
This is beacuse the structure is used internally, rather than being a published API. That said, efforts will be made
to indicate in the release notes if fields in the structure are renamed or moved, but not for adding new
items.

## Prior Art

The `bluetooth_tracker` and `ble_tracker` integrations are only built to give a "home/not home"
determination, and don't do "Area" based location. (nb: "Zones" are places outside the
home, while "Areas" are rooms/areas inside the home). I wanted to be free to experiment with
this in ways that might not suit core, but hopefully at least some of this could find
a home in the core codebase one day.

The "monitor" script uses standalone Pi's to gather bluetooth data and then pumps it into
MQTT. It doesn't use the `bluetooth_proxy` capabilities which I feel are the future of
home bluetooth networking (well, it is for my home, anyway!).

ESPresense looks cool, but I don't want to dedicate my nodes to non-esphome use, and again
it doesn't leverage the bluetooth proxy features now in HA. I am probably reinventing
a fair amount of ESPresense's wheel.

## Installation

Install this fork through its HACS custom repository as described in [Install this fork with HACS](#install-this-fork-with-hacs). Searching the default catalog can instead select upstream Bermuda. The button below opens the BenCodez repository.

[![Open this fork in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=BenCodez&repository=bermuda&category=Integration)

You should now be able to add the `Bermuda BLE Trilateration` integration. Once you have done that,
you need to restart Home Assistant, then in `Settings`, `Devices & Services` choose `Add Integration`
and search for `Bermuda BLE Trilateration`. It's possible that it will autodetect for you just by
noticing nearby bluetooth devices.

Once the integration is added, you need to set up your devices by clicking `Configure` in `Devices and Services`,
`Bermuda BLE Trilateration`.

In the `Configuration` dialog, you can choose which bluetooth devices you would like the integration to track.

You can manually install Bermuda by doing the following:

1. Using the tool of choice open the directory (folder) for your HA configuration (where you find `configuration.yaml`).
2. If you do not have a `custom_components` directory (folder) there, you need to create it.
3. In the `custom_components` directory (folder) create a new folder called `bermuda`.
4. Download _all_ the files from the `custom_components/bermuda/` directory (folder) in this repository.
5. Place the files you downloaded in the new directory (folder) you created.
6. Restart Home Assistant
7. In the HA UI go to "Configuration" -> "Integrations" click "+" and search for "Bermuda BLE Trilateration"

<!---->

## Contributions are welcome!

If you want to contribute to this please read the [Contribution guidelines](CONTRIBUTING.md)

## Credits

This project was generated from [@oncleben31](https://github.com/oncleben31)'s [Home Assistant Custom Component Cookiecutter](https://github.com/oncleben31/cookiecutter-homeassistant-custom-component) template.

Code template was mainly taken from [@Ludeeus](https://github.com/ludeeus)'s [integration_blueprint][integration_blueprint] template
[Cookiecutter User Guide](https://cookiecutter-homeassistant-custom-component.readthedocs.io/en/stable/quickstart.html)\*\*

---

[integration_blueprint]: https://github.com/custom-components/integration_blueprint

[black]: https://github.com/psf/black
[black-shield]: https://img.shields.io/badge/code%20style-black-000000.svg?style=for-the-badge

[buymecoffee]: https://www.buymeacoffee.com/AshleyGittins
[buymecoffeebadge]: https://img.shields.io/badge/buy%20me%20a%20coffee-Caffeinate-green.svg?style=for-the-badge

[commits-shield]: https://img.shields.io/github/commit-activity/y/BenCodez/bermuda.svg?style=for-the-badge
[commits]: https://github.com/BenCodez/bermuda/commits/main

[hacs]: https://hacs.xyz
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-blue.svg?style=for-the-badge

[haminver]: https://github.com/agittins/bermuda/commits/main/hacs.json
[haminverbadge]: https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fgithub.com%2Fagittins%2Fbermuda%2Fraw%2Fmain%2Fhacs.json&query=%24.homeassistant&style=for-the-badge&logo=homeassistant&logoColor=%2311BDF2&label=Minimum%20HA%20Version

[discord]: https://discord.gg/Qa5fW2R
[discord-shield]: https://img.shields.io/discord/330944238910963714.svg?style=for-the-badge

[exampleimg]: example.png
[forum-shield]: https://img.shields.io/badge/community-forum-brightgreen.svg?style=for-the-badge
[forum]: https://community.home-assistant.io/

[license-shield]: https://img.shields.io/github/license/agittins/bermuda.svg?style=for-the-badge
[maintenance-shield]: https://img.shields.io/badge/maintainer-%40agittins-blue.svg?style=for-the-badge

[patreon]: https://patreon.com/AshGittins
[patreonbadge]: https://img.shields.io/badge/Patreon-Sponsor-green?style=for-the-badge

[pre-commit]: https://github.com/pre-commit/pre-commit
[pre-commit-shield]: https://img.shields.io/badge/pre--commit-enabled-brightgreen?style=for-the-badge

[sponsorsbadge]: https://img.shields.io/github/sponsors/agittins?style=for-the-badge&label=GitHub%20Sponsors&color=green
[sponsors]: https://github.com/sponsors/agittins

[releases-shield]: https://img.shields.io/github/release/agittins/bermuda.svg?style=for-the-badge
[releases]: https://github.com/agittins/bermuda/releases
[user_profile]: https://github.com/agittins
