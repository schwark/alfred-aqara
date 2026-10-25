# Aqara Mobile — parallel experiment

This branch builds a separate Alfred workflow named **Aqara Mobile
(Experimental)** with keyword **`aqm`**. The existing `aq` workflow remains
installed with its own accounts, data, credentials, and stable update channel.

The experiment combines the mobile app's common lock-command API with an
explicit Agent fallback. Importing the existing Agent profiles preserves their
device commands, scenes, home switching, status subtitles, autocomplete, and
notifications. Native mobile discovery adds devices and capability-advertised
lock/unlock commands. **This is not yet a complete mobile-only replacement.**

## Build and connect

```sh
python3 -m unittest discover -s tests -v
python3 workflow-build --mobile
open dist/Aqara-Mobile-0.3.0-mobile.1.alfredworkflow
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

The runtime still needs only Python 3.9+ and its standard library. Emulator,
Frida, mitmproxy, APKs, and the research tools are not runtime dependencies.

## What is retained

| Feature | Parallel implementation |
| --- | --- |
| Accounts, regions, multiple homes, rename, rollback | Shared profile/home implementation; isolated data and Keychain service |
| Search, status subtitle, duplicate names, Tab completion | Shared Alfred interface |
| Lights, switches, curtains, AC controls | Existing Agent routes retained after import |
| Existing scenes | Existing Agent route retained |
| Additional mobile scenes | App scene list and execution route; construction verified in app code, live execution unverified |
| Mobile lock/unlock | Per-device DoorLock command metadata plus common `/matter/write` request |
| Other mobile-only devices | Named-resource status; native controls not implemented yet |
| Workflow updates | Same verified updater, isolated to `Aqara-Mobile-*` assets; mobile prereleases enabled |

Homes are associated across APIs only by an exact home ID. Devices are
associated only by an exact device ID. Similar names are never used to route
a write. Unmatched mobile devices remain separate entries; a matching display
name gets a `(Mobile)` suffix. If the APIs expose different IDs, select the
mobile home/device explicitly. Multi-endpoint locks receive separate entries.

## Hub-first investigation

The next transport target is the M3 itself. The current lock-plugin paths do
not establish that hub control requires phone-to-lock pairing. See the
[M3 hub research](https://github.com/schwark/alfred-aqara/blob/codex/aqara-mobile-api/docs/aqara-hub-api-research.md).
The installed experiment currently retains the Agent baseline while this hub
transport is validated; it does not yet implement LANLink control.

## Lock behavior and limitations

The common Android helper builds this signed request:

```text
POST /app/v1.0/lumi/matter/write
{did, pwd, data: {"<endpoint>.148.<command>.0": ""}, type: 0}
```

Only `35010` (lock) and `35011` (unlock) are supported. The workflow discovers
the endpoint and supported commands from **that device's** `/matter/dev/detail`
response, and checks them again at execution. It does not infer capabilities
from a model name, another lock, or the global specification.

Selecting a lock action requests the device's separate remote-control password
in a hidden dialog. Leave it empty if no password is configured. A nonempty
password is encoded as the lowercase MD5 expected by the shared app helper;
it is not saved. Cancel sends no write. A server rejection or timeout sends no
retry and does not fall back to a second transport. Notification text describes
the request; it does not claim the physical lock changed state.

**Live lock execution has not been verified.** App-code request construction
and read-only capability/status discovery are the evidence so far. U300 uses
the shared Matter path in the app. U100's normal plugin path uses a paired
encrypted tunnel; pairing/key provisioning and that legacy transport are not
implemented. A device without advertised common lock commands remains
read-only. Full native parity, including U100 control, is an open result of
the experiment rather than a claim made by this build.

Browsing, refresh, import, and parity inspection never issue device writes.
Lock commands and scene runs occur only after selecting their Alfred actions.

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
