# Aqara Cloud mode

Cloud mode uses the Aqara Home app's cloud API, including lock and unlock on
supported endpoints. It is part of the same **Aqara** workflow and uses the same
**`aq`** keyword as Agent mode. **Agent is the default.**

```text
aq mode          Choose Agent or Cloud and see the current mode
aq mode cloud    Switch to Cloud
aq mode agent    Switch back to Agent
```

The choice is saved across Alfred sessions. Each mode keeps its own connections,
selected account and home, catalogs, and status preferences. Switching modes
sends no device commands. A command selected before switching must be selected
again afterward.

U100 and U300 cloud unlock followed by lock have been verified with matching
reported-state changes. The tested U100 route works through its M3 without
phone-to-lock pairing keys. Capability discovery uses Aqara's current endpoint
definitions and device metadata rather than bundled drivers for each model.

Cloud mode lists **only devices returned by the Cloud API**. Agent-only devices
stay in Agent mode. Importing Agent profiles supplies credentials and fallback
metadata; it does not add their devices to the Cloud list.

Native Cloud commands cover lock/unlock, power, brightness, and color
temperature where advertised. An exact Agent endpoint match can add fallback
controls such as color, curtains, and climate to the same Cloud device row. This mode uses
undocumented app APIs and remains experimental.

## Requirements

- macOS with Alfred 5 and a Powerpack license.
- Python 3.9 or newer; no additional Python packages are required.
- An Aqara Home account with devices in the account's selected region.
- An optional Aqara Agent connection for additional controls on exactly matched Cloud endpoints.

The Mac needs internet access. A hub IP address, Home Assistant, an Android
emulator, and capture tools are not required at runtime. Cloud mode is a connection
option; it does not require a second workflow or keyword.

## Connect Cloud mode

Install [Aqara-0.3.5.alfredworkflow](https://github.com/schwark/alfred-aqara/releases/download/v0.3.5/Aqara-0.3.5.alfredworkflow)
from [GitHub Releases](https://github.com/schwark/alfred-aqara/releases/latest).
Both modes are included. To build from the repository instead:

```sh
python3 workflow-build
open dist/Aqara-0.3.5.alfredworkflow
```

1. Run `aq mode cloud`. If the earlier parallel workflow was configured,
   its saved connections, home links, and signing identity are copied into Cloud
   mode automatically. Otherwise, run `aq import` before adding Cloud accounts
   to copy your Agent profiles, cached catalogs, and fallback keys. Agent mode
   keeps its existing setup.
2. Run `aq login` for each profile that needs a Cloud session. The workflow
   automatically downloads and caches its shared app-signing configuration,
   so a new Mac needs no manual identity download or import. Select an existing
   profile with `aq account` before connecting it. Migrated sessions can be used
   immediately.
   Without imported profiles, use `aq login` for the initial connection.
   Enter the Aqara Home area, two-letter country, username, and password in
   dialogs. Choose the region used by that account in Aqara Home:

   | Aqara Home region | Area | Country |
   | --- | --- | --- |
   | India | `SEA` | `IN` |
   | United States | `US` | `US` |
   | Other supported areas | `EU`, `CN`, or `KR` | Account country code |

3. Run `aq home` to select a home, then `aq update` to refresh its devices
   and scenes. Run `aq parity` to inspect native commands and Agent fallback
   coverage. Repeat login for each imported account/region.
4. If the same home appears separately from Agent and Cloud discovery,
   select its Agent entry and run `aq home link` to choose the Cloud
   counterpart. Run `aq update` afterward. Use `aq home unlink` to remove
   that association.

The account password is used once for sign-in and then discarded. Only the
returned session and routing fields are saved in Keychain. Sessions are not
refreshed automatically; run `aq login` again if a session expires or is rejected.
macOS may ask you to allow Python to access the Keychain items.

## Upgrading from the parallel workflow

Install the unified Aqara workflow into the existing `aq` entry, then run
`aq mode cloud`. On the first switch, the workflow copies the earlier parallel
setup's profiles, catalogs, home links, sessions, and signing identity into the
new Cloud data directory and Keychain service. Existing Cloud data is not
overwritten, and a failed migration removes its newly copied credentials.
The earlier setup remains available for recovery.

Migrated sessions can be used immediately; run `aq login` only when reconnecting
an expired or rejected session. Do not run `aq import` after migration, since
it is only for an unconfigured Cloud mode. Once the unified setup works, disable
or remove the earlier parallel workflow in Alfred to retire its old keyword.
The workflow migrates data; it does not remove another installed workflow.

## Daily use

These commands use the selected mode. Lock controls and Cloud setup commands
are available in Cloud mode.

Type `aq` to browse, or type part of a device, room, or scene name. A search
matching one device opens its commands. Choices have distinct titles such as
“Turn on,” “Turn off,” “Lock,” and “Unlock”; the subtitle identifies the device
and home. Status appears as a readable subtitle instead of raw JSON.

Tab completes device names, commands, and suggested values. Commands needing
a value keep a trailing space so you can continue typing. Duplicate device
names require selecting the exact result before running a command. Use
`aq device <name>` when a device name conflicts with a workflow command.

| Command | Behavior |
| --- | --- |
| `aq mode` / `aq mode agent` / `aq mode cloud` | View or change the operating mode |
| `aq <search>` | Find devices, rooms, types, and scenes |
| `aq <device> status` | Read the current reported state |
| `aq <lock> lock` / `unlock` | Request lock control where the endpoint advertises it |
| `aq <device> on` / `off` | Power control where supported |
| `aq <light> dim 50` | Set brightness |
| `aq <light> ct 4000` | Set color temperature in kelvin |
| `aq <light> color blue` | Set color through Agent fallback |
| `aq <curtain> open` / `close` / `stop` / `slevel 50` | Curtain controls through Agent fallback |
| `aq <air conditioner> mode cool` / `temp 24` | Climate controls through Agent fallback |
| `aq scene <search>` | Find a saved scene; Return requests execution |
| `aq showstatus on` / `off` | Show or hide live status above a device's commands |
| `aq update` | Refresh the selected account's homes and current catalog |
| `aq parity` | Inspect native capability discovery and retained Agent coverage |

Existing scenes retain their Agent execution route. Additional Cloud scenes
use the app's scene API; that request construction has been verified in app
code, but live execution remains unverified.

Notifications describe the requested action. Use `status` to check the resulting
reported state. Browsing, import, refresh, and coverage inspection do not issue
device writes. A failed native write is never retried through Agent.

## Device and channel names

Cloud mode reads Aqara's endpoint panels in the selected account and home.
Composite switches use the individual channel names configured in Aqara Home,
instead of appending “Endpoint 2” to the parent switch name. Endpoint IDs remain
bound to their declared commands, and duplicate names retain room labels.
Generic endpoint labels are used only when Aqara provides no channel name.

A live four-button Z1 Pro returned four named channels: three power relays and
one wireless button. The three relay channels offer on/off; the wireless button
remains read-only. Other models use the same metadata-driven discovery.
Run `aq update` after renaming channels in Aqara Home.

A Cloud refresh can take several seconds while it reads device capabilities.
A start notification appears immediately; reopen `aq upd` to see progress and,
after completion, the timestamp and device/scene counts. Device refresh stays
first, followed by workflow update, including when a new release is available.

## Multiple homes, accounts, and regions

`aq home` lists homes across saved accounts and regions, with account labels
to distinguish them. Each connection remembers its selected home and catalogs.
Selecting another home restores or loads its catalog; a failed switch restores
the previous selection.

- `aq account add <label>` creates another connection and prompts for its
  Cloud login. Use a separate profile for each account/region combination.
- `aq account` switches profiles; `aq account rename` lets you choose and
  rename a profile without connecting to it. `aq account rename <label>`
  renames the current profile.
- `aq apikey` adds or replaces the selected profile's Agent fallback key,
  obtained from [Aqara Agent](https://agent.aqara.com/login). Run `aq update`
  after connecting it.
- `aq home <search>` filters homes by name or account label.
  `aq home update` discovers homes across all saved connections.

Home links are explicit when API IDs differ; similar names never create a
link automatically. Cloud devices use their Cloud names and identifiers. A
fallback is attached only by an exact device ID or a unique returned endpoint
identifier matching the Agent endpoint ID; names never attach controls.
Unmatched Agent devices remain accessible with `aq mode agent`. Different Cloud
devices with the same name use room labels, and multi-endpoint devices can have
separate entries. Existing mixed caches are filtered automatically, including
cached catalogs restored when switching homes.

Matched devices keep their original Agent commands. Native power, brightness,
and color-temperature controls are used when advertised and compatible with
the requested value. Otherwise the existing Agent route can be selected before
the write. A failure never triggers a second transport automatically.

## Lock control

Select the exact lock and choose **Unlock** or **Lock**. Both use the same
regional cloud command route; the U100's tested path reaches it through its M3.
No direct LAN connection is required.

Commands are offered only when Aqara's endpoint metadata declares the common
DoorLock commands. Matter devices use their device capabilities; Zigbee/hub
devices use current server definitions confirmed by a live lock-state read.
Execution rechecks the model, home, endpoint, and capabilities. Unknown
capabilities remain read-only.

If the device metadata says a remote-control password is configured, a hidden
dialog asks for that separate **device password**, not your Aqara account
password. Leave it empty if none is configured. It is used for that request
and is not saved. Cancel sends no command. A rejection or timeout is not retried.

Live tests sent one Unlock followed by one Lock to each tested U100 and U300.
Both requests were accepted, and fresh reads reported the requested states.
Those tests establish cloud acceptance and status readback; mechanical movement
was not independently observed. Other device/firmware combinations have not
been verified. Details are in the
[Cloud API notes](https://github.com/schwark/alfred-aqara/blob/main/docs/aqara-cloud-api-research.md)
and [M3 hub investigation](https://github.com/schwark/alfred-aqara/blob/main/docs/aqara-hub-api-research.md).

## Updates

Both modes share the same updater and stable GitHub Releases with `Aqara-*`
assets. The earlier parallel packages are ignored. Update checks run daily while
using either mode; the chosen mode and each mode's connections survive upgrades.

- `aq workflow:update` checks for an update and opens Alfred's import dialog
  after verifying the download's SHA-256, bundle identity, and version.
- `aq workflow:autoupdate` enables automatic checks, the default.
- `aq workflow:noautoupdate` disables automatic checks.

`aq up` offers both device refresh and workflow update; `aq update` continues
to refresh devices. `aq work` and `aq workflow:u` also offer Tab completion to
`workflow:update` in either mode. Results refresh while a background check runs. The first use after
an upgrade checks again; manual updates always fetch GitHub's current release list.

Complete an upgrade in Alfred's import dialog. Saved accounts, home links,
and Keychain credentials live outside the installed workflow and survive
upgrades. `aq update` refreshes devices and scenes; it does not upgrade the workflow.

## Automatic signing setup and recovery

The “Cloud identity” is shared app configuration, not a personal identity or a
lock-pairing key. Starting with v0.3.2, the first Cloud use automatically fetches
[the pinned public reference](https://github.com/home-assistant-plugins/aquara-local/blob/4fc274688cd40774307549172c9ae1db073c92ca/custom_components/aquara_local/const.py),
checks its SHA-256 and size, and parses only the required literal constants.
The Python source is never executed or retained as a downloaded file. The
request sends no Aqara account credentials, tokens, or signing headers to GitHub.

Only the parsed values and reference hash are cached in macOS Keychain. Later
uses need no GitHub request while that cache matches the pinned reference.
A new Mac recovers the same configuration automatically when you run `aq login`;
you do not need the old laptop or a copy of `const.py`. Initial setup requires
access to GitHub and the pinned reference; a temporary fetch failure asks you
to retry login rather than handling files manually.

The app values are not bundled in releases or committed source. Source contains
only the reference URL/hash and loader; attribution is in `cloud-identity.LICENSE`.
Personal passwords and sessions remain separate. You sign in again on a
replacement Mac. No developer-account registration or lock pairing is needed
for this configuration.

`aq identity` remains an advanced manual override for a custom `const.py` or
JSON configuration. Normal setup does not use it, and it is omitted from the
default command list. Existing valid Keychain overrides continue to work.

## Local data and privacy

The workflow bundle is `com.schwark.aqara`. Cloud credentials use the separate
Keychain service `com.schwark.aqara.cloud`, with accounts `connection:<profile-id>`,
an optional `app-identity` advanced override, and `public-app-identity` for
the automatically fetched shared configuration and reference hash. Cloud account
labels, catalogs, home links, and five-second status caches live under:

```text
~/Library/Application Support/Alfred/Workflow Data/com.schwark.aqara/cloud/
```

`aq reinit` resets only the selected mode's connections, credentials, home
selections, and catalogs. The shared configuration cache and any advanced
Keychain override remain available for reconnecting accounts. Agent mode's data
stays in the parent workflow data directory, and `mode.json` stores the
operating-mode choice.

Packages use an explicit file allowlist. They contain no saved accounts,
home/device records, account passwords, session tokens, Agent API keys, APKs,
or traffic captures. Shared app values are fetched from the public reference
and kept in Keychain, not included in the package.
Research tools are excluded from the workflow; see the
[Cloud API research notes](https://github.com/schwark/alfred-aqara/blob/main/docs/aqara-cloud-api-research.md)
for protocol details and read-only capture tools.
