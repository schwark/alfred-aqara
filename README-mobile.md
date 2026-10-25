# Aqara Mobile for Alfred

Control Aqara devices through the Aqara Home mobile app's cloud API, including
lock and unlock on supported endpoints. This experimental mode installs as
**Aqara Mobile (Experimental)** with keyword **`aqm`**, alongside the stable
[`aq` workflow](https://github.com/schwark/alfred-aqara/blob/codex/aqara-mobile-api/README.md).
It has separate accounts, local data, Keychain credentials, and an update channel.

U100 and U300 cloud unlock followed by lock have been verified with matching
reported-state changes. The tested U100 route works through its M3 without
phone-to-lock pairing keys. Capability discovery uses Aqara's current endpoint
definitions and device metadata rather than bundled drivers for each model.

Importing your existing Agent profiles preserves their controls and scenes.
Native mobile commands cover lock/unlock, power, brightness, and color
temperature where advertised. **Full command coverage still uses Agent
fallback** for controls such as color, curtains, and climate. This mode uses
undocumented mobile APIs and remains experimental.

## Requirements

- macOS with Alfred 5 and a Powerpack license.
- Python 3.9 or newer; no additional Python packages are required.
- An Aqara Home account with devices in the account's selected region.
- A separately obtained app-identity reference for signing mobile API requests.
- An Aqara Agent connection to retain controls outside the native mobile coverage.

The Mac needs internet access. A hub IP address, Home Assistant, an Android
emulator, and capture tools are not required at runtime.

## Install and connect

From the `codex/aqara-mobile-api` branch:

```sh
python3 workflow-build --mobile
open dist/Aqara-Mobile-0.3.0-mobile.2.alfredworkflow
```

1. To keep your current Agent setup, run `aqm import` before configuring any
   accounts in this workflow. It copies profiles, cached catalogs, and Keychain
   keys locally. The original `aq` workflow remains unchanged.
2. Run `aqm identity` and select the external `const.py` containing `APPID`,
   `APPKEY`, `RSA_N`, and `RSA_E`. The reference used during development is
   [aquara-local at this pinned revision](https://github.com/home-assistant-plugins/aquara-local/blob/4fc274688cd40774307549172c9ae1db073c92ca/custom_components/aquara_local/const.py).
   Only literal assignments are read; the file is never executed. The imported
   app identity is saved in Keychain and is excluded from source and packages.
3. Select an imported profile with `aqm account`, then run `aqm login`.
   Without imported profiles, use `aqm login` for the initial connection.
   Enter the Aqara Home area, two-letter country, username, and password in
   dialogs. Choose the region used by that account in Aqara Home:

   | Aqara Home region | Area | Country |
   | --- | --- | --- |
   | India | `SEA` | `IN` |
   | United States | `US` | `US` |
   | Other supported areas | `EU`, `CN`, or `KR` | Account country code |

4. Run `aqm home` to select a home, then `aqm update` to refresh its devices
   and scenes. Run `aqm parity` to inspect native commands and Agent fallback
   coverage. Repeat login for each imported account/region.
5. If the same home appears separately from Agent and mobile discovery,
   select its Agent entry and run `aqm home link` to choose the mobile
   counterpart. Run `aqm update` afterward. Use `aqm home unlink` to remove
   that association.

The account password is used once for sign-in and then discarded. Only the
returned session and routing fields are saved in Keychain. Sessions are not
refreshed automatically; run `aqm login` again if a session expires or is rejected.
macOS may ask you to allow Python to access the Keychain items.

## Daily use

Type `aqm` to browse, or type part of a device, room, or scene name. A search
matching one device opens its commands. Choices have distinct titles such as
“Turn on,” “Turn off,” “Lock,” and “Unlock”; the subtitle identifies the device
and home. Status appears as a readable subtitle instead of raw JSON.

Tab completes device names, commands, and suggested values. Commands needing
a value keep a trailing space so you can continue typing. Duplicate device
names require selecting the exact result before running a command. Use
`aqm device <name>` when a device name conflicts with a workflow command.

| Command | Behavior |
| --- | --- |
| `aqm <search>` | Find devices, rooms, types, and scenes |
| `aqm <device> status` | Read the current reported state |
| `aqm <lock> lock` / `unlock` | Request lock control where the endpoint advertises it |
| `aqm <device> on` / `off` | Power control where supported |
| `aqm <light> dim 50` | Set brightness |
| `aqm <light> ct 4000` | Set color temperature in kelvin |
| `aqm <light> color blue` | Set color through Agent fallback |
| `aqm <curtain> open` / `close` / `stop` / `slevel 50` | Curtain controls through Agent fallback |
| `aqm <air conditioner> mode cool` / `temp 24` | Climate controls through Agent fallback |
| `aqm scene <search>` | Find a saved scene; Return requests execution |
| `aqm showstatus on` / `off` | Show or hide live status above a device's commands |
| `aqm update` | Refresh the selected account's homes and current catalog |
| `aqm parity` | Inspect native capability discovery and retained Agent coverage |

Existing scenes retain their Agent execution route. Additional mobile scenes
use the app's scene API; that request construction has been verified in app
code, but live execution remains unverified.

Notifications describe the requested action. Use `status` to check the resulting
reported state. Browsing, import, refresh, and coverage inspection do not issue
device writes. A failed native write is never retried through Agent.

## Multiple homes, accounts, and regions

`aqm home` lists homes across saved accounts and regions, with account labels
to distinguish them. Each connection remembers its selected home and catalogs.
Selecting another home restores or loads its catalog; a failed switch restores
the previous selection.

- `aqm account add <label>` creates another connection and prompts for its
  mobile login. Use a separate profile for each account/region combination.
- `aqm account` switches profiles; `aqm account rename` lets you choose and
  rename a profile without connecting to it. `aqm account rename <label>`
  renames the current profile.
- `aqm apikey` adds or replaces the selected profile's Agent fallback key,
  obtained from [Aqara Agent](https://agent.aqara.com/login). Run `aqm update`
  after connecting it.
- `aqm home <search>` filters homes by name or account label.
  `aqm home update` discovers homes across all saved connections.

Home links are explicit when API IDs differ; similar names never create a
link automatically. Devices are combined only by exact device IDs or a unique
returned endpoint identifier matching the Agent endpoint ID. Unmatched mobile
devices remain separate; a duplicate display name gets a `(Mobile)` suffix.
Multi-endpoint devices can have separate entries.

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
[mobile API notes](https://github.com/schwark/alfred-aqara/blob/codex/aqara-mobile-api/docs/aqara-mobile-api-research.md)
and [M3 hub investigation](https://github.com/schwark/alfred-aqara/blob/codex/aqara-mobile-api/docs/aqara-hub-api-research.md).

## Updates

`aqm` checks GitHub Releases daily while you use it and accepts mobile
prereleases with `Aqara-Mobile-*` assets. Stable `aq` and mobile `aqm` use
separate update channels and cannot replace each other through the updater.

- `aqm workflow:update` checks for an update and opens Alfred's import dialog
  after verifying the download's SHA-256, bundle identity, and version.
- `aqm workflow:autoupdate` enables automatic checks, the default.
- `aqm workflow:noautoupdate` disables automatic checks.

Complete an upgrade in Alfred's import dialog. Saved accounts, home links,
and Keychain credentials live outside the installed workflow and survive
upgrades. `aqm update` refreshes devices and scenes; it does not upgrade the workflow.

## Local data and privacy

The bundle and Keychain service are `com.schwark.aqara.mobile`. Connection
secrets use Keychain accounts `connection:<profile-id>`; the imported signing
identity uses `app-identity`. Account labels, catalogs, home links, and five-second
status caches live under:

```text
~/Library/Application Support/Alfred/Workflow Data/com.schwark.aqara.mobile/
```

`aqm reinit` removes saved connections, credentials, selections, and catalogs.
The shared app identity remains in Keychain for reconnecting accounts.

Packages use an explicit file allowlist. They contain no saved accounts,
home/device records, app constants, credentials, APKs, or traffic captures.
Research tools are excluded from the workflow; see the
[API research notes](https://github.com/schwark/alfred-aqara/blob/codex/aqara-mobile-api/docs/aqara-mobile-api-research.md)
for protocol details and read-only capture tools.
