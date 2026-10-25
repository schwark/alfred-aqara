# Aqara Mobile — parallel experiment

This branch builds a separate Alfred workflow named **Aqara Mobile
(Experimental)** with keyword **`aqm`**. The existing `aq` workflow remains
installed with its own accounts, data, credentials, and stable update channel.

The experiment combines the mobile app's shared cloud trait/command APIs with an
explicit Agent fallback. Importing the existing Agent profiles preserves their
device commands, scenes, home switching, status subtitles, autocomplete, and
notifications. Native mobile discovery adds devices and capability-advertised
lock/unlock commands, plus advertised power, brightness, and color-temperature
traits. **This is not yet a complete mobile-only replacement.**

## Build and connect

```sh
python3 -m unittest discover -s tests -v
python3 workflow-build --mobile
open dist/Aqara-Mobile-0.3.0-mobile.2.alfredworkflow
```

1. Run `aqm import` before configuring any accounts. It copies the current
   Agent profiles and cached catalogs to the parallel workflow. Credentials
   are copied between local Keychain items; the original workflow is unchanged.
   Import is local and does not send any device commands.
2. Run `aqm identity` and select a separately obtained app-identity reference
   `const.py` containing `APPID`, `APPKEY`, `RSA_N`, and `RSA_E`. Only literal
   assignments are parsed; the reference is never executed. Its constants are
   stored in Keychain, and are excluded from source and release packages. The
   research reference and its pinned revision are documented in
   [the API notes](https://github.com/schwark/alfred-aqara/blob/codex/aqara-mobile-api/docs/aqara-mobile-api-research.md).
3. Select a saved account with `aqm account`, then run `aqm login`. Enter the
   Aqara Home area, two-letter country, username, and password in dialogs.
   India uses area `SEA` and country `IN`; the United States uses `US` / `US`.
   Other supported areas are `EU`, `CN`, and `KR`. Choose the region used by
   that account in Aqara Home. These are examples, not personal mappings.
4. The password is used for sign-in and then discarded. Only the returned
   session and routing fields are kept in Keychain. Sessions are not refreshed
   automatically; use `aqm login` again if Aqara expires or rejects one.
5. Run `aqm home` and `aqm update`. Use `aqm parity` to inspect command coverage.
   Repeat mobile login for each imported account/region. `aqm account add
   <label>` also supports a new mobile-only connection. `aqm apikey` connects
   an Agent fallback to an existing profile.
6. If Agent and mobile home IDs differ, select the Agent home, then use
   `aqm home link` to select its mobile counterpart. Run `aqm update` afterward.
   `aqm home unlink` removes that explicit association. Names do not create
   links automatically.

The runtime still needs only Python 3.9+ and its standard library. Emulator,
Frida, mitmproxy, APKs, and the research tools are not runtime dependencies.

## What is retained

| Feature | Parallel implementation |
| --- | --- |
| Accounts, regions, multiple homes, rename, rollback | Shared profile/home implementation; isolated data and Keychain service |
| Search, status subtitle, duplicate names, Tab completion | Shared Alfred interface |
| Lights, switches, curtains, AC controls | Existing Agent routes retained after import; matched endpoints may use common native power, brightness, and color-temperature traits |
| Existing scenes | Existing Agent route retained |
| Additional mobile scenes | App scene list and execution route; construction verified in app code, live execution unverified |
| Mobile lock/unlock | Common `/matter/write` request; U100 and U300 unlock/lock verified with reported-state changes |
| Other mobile-only devices | Known trait status, power, brightness, and color temperature where advertised; unknown capabilities remain read-only |
| Workflow updates | Same verified updater, isolated to `Aqara-Mobile-*` assets; mobile prereleases enabled |

Homes are associated by an exact home ID or an explicit saved home link. Devices are
associated only by an exact device ID or a unique returned endpoint `subjectKey`
matching the Agent endpoint ID. Similar names are never used to route
a write. Unmatched mobile devices remain separate entries; a matching display
name gets a `(Mobile)` suffix. If the APIs expose different IDs, select the
mobile home/device explicitly. Multi-endpoint locks receive separate entries.
Matched endpoints retain their original Agent commands, including color,
curtain, and climate controls. A value outside the native device's range may
use the existing Agent route when supported; this choice happens before a
write. A failed native operation never triggers an Agent retry.

## Hub-first investigation

The primary transport target is **Alfred → Aqara cloud → M3 → device**. The
current lock-plugin paths do
not establish that hub control requires phone-to-lock pairing. See the
[M3 hub research](https://github.com/schwark/alfred-aqara/blob/codex/aqara-mobile-api/docs/aqara-hub-api-research.md).
The experiment reads endpoint panels and traits for Zigbee/hub devices as well
as Matter metadata. Its native writer uses the regional cloud endpoints;
LANLink control is not a dependency. The U100 and U300 have both accepted
Unlock followed by Lock through this shared cloud API, with matching
status readback.

## Lock behavior and limitations

The common Android helper builds this signed request:

```text
POST /app/v1.0/lumi/matter/write
{did, pwd, data: {"<endpoint>.148.<command>.0": ""}, type: 0}
```

Only `35010` (lock) and `35011` (unlock) are supported. Matter devices advertise
commands through `/matter/dev/detail`. Zigbee devices use Aqara's downloaded
endpoint definitions for their inventory model, confirmed by a live device
LockState trait read. Definitions come from Aqara's current gzip data rather
than bundled per-model drivers. A global command list alone is insufficient.
Execution rechecks the device's model, home, exact endpoint, and capabilities.
The U100 can expose `RemoteControlWithAuthCode` alongside these working cloud
commands; that trait's presence does not invalidate the shared command route.

When device metadata says no remote-control password is configured, commands
use an empty password. Otherwise a hidden dialog requests this separate
device password; leave it empty if none is configured. A nonempty
password is encoded as the lowercase MD5 expected by the shared app helper;
it is not saved. Cancel sends no write. A server rejection or timeout sends no
retry and does not fall back to a second transport. Notification text describes
the request; it does not claim the physical lock changed state.

Live tests sent one Unlock and one Lock to each tested model. Both requests
were accepted and fresh reads confirmed the requested states. No paired
phone key was used for the tested U100 cloud route. The app's separate
encrypted-plugin branch remains documented, but is not needed for this route.
Other device/firmware combinations are not proven by those tests. A device
without declared common commands and matching live metadata remains read-only.
Full mobile-only parity still needs Agent fallback for unsupported commands.

Browsing, refresh, import, and parity inspection never issue device writes.
Lock commands and scene runs occur only after selecting their Alfred actions.

## Read-only cloud evidence

After connecting accounts, run `python3 tools/cloud_probe.py --devices` from
the research checkout to read all homes, device panels, traits, extended
metadata, and the shared specification. It uses the saved Keychain sessions
and has no device-write path. Its console output contains only counts and
safe error codes. Detailed evidence stays in the private file
`~/.cache/alfred-aqara/cloud-evidence.json` with mode `0600`; it is excluded
from source and workflow packages. Without mobile sessions, it reports
`sign_in_required` instead of trying credentials or logging private data.
Add `--bridges` to also query cloud bridge eligibility, gateway details,
bridge subdevices, and hub Matter endpoints. This reads existing relationships;
it does not bind or unbind devices. Eligibility alone does not prove that a
hub-owned lock command is exposed through the cloud.

## Data and updates

The parallel bundle and Keychain service are `com.schwark.aqara.mobile`.
Connection secrets use Keychain accounts `connection:<profile-id>`; the
separately obtained signing identity uses `app-identity`. Account labels,
catalogs, home selections, and five-second status caches live outside the
installed workflow in Alfred's Workflow Data directory for this bundle.
`aqm reinit` removes connection credentials and local catalogs; the shared
app identity remains available for reconnecting accounts.

Stable `aq` ignores mobile assets and prereleases. `aqm` ignores stable assets
and accepts mobile prereleases. Both verify the GitHub SHA-256, bundle identity,
and version before opening Alfred's import dialog. The mobile update cache is
independent. A release package contains only an explicit source-file allowlist;
no accounts, home/device records, captures, app constants, or credentials.
