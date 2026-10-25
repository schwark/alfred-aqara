# alfred-aqara

Control Aqara devices and run saved scenes from Alfred. Inspired by
Alfred home automation workflows, with the same
device-first command style.

The default **Agent mode** uses **Aqara Agent's official cloud API**. Your Hub M3 and its
devices must be registered in Aqara Home. The Mac needs internet access;
the hub's IP address and a Home Assistant installation are not required.
Availability depends on the devices and account region exposed by Aqara Agent.
This is not a direct LAN or Matter controller.

One keyword, `aq`, supports both **Agent** and **Cloud** modes. Run `aq mode`
to choose, `aq mode cloud` to enable Cloud, or `aq mode agent` to switch back.
Agent is the default, and the choice is remembered. Cloud mode adds supported
lock/unlock commands; see [README-cloud.md](README-cloud.md) for authentication,
capabilities, and setup. Each mode keeps its own accounts and selected home.

## Requirements

- macOS with Alfred 5 and a Powerpack license.
- Python 3.9 or newer, installed from [python.org](https://www.python.org/downloads/macos/)
  or Homebrew (`brew install python`). No Python packages are required.
- An Aqara account that can sign in to [Aqara Agent](https://agent.aqara.com/login).

## Install and connect

These steps connect the default Agent mode. For Cloud, switch with `aq mode cloud`
and follow [the Cloud setup guide](README-cloud.md#connect-cloud-mode).

1. Build the workflow with `python3 workflow-build`, then open
   `dist/Aqara-0.3.0.alfredworkflow` to import it into Alfred.
2. Type `aq login` in Alfred and press Return. Sign in with your Aqara account.
3. Copy the **aqara_api_key** shown on the page. Type `aq apikey` in Alfred and
   press Return to paste it into the secure dialog. It is saved in macOS Keychain.
   The key is never entered in Alfred's search field or stored in the workflow.
4. Run `aq update`. If the account has one home it is selected automatically.
   With multiple homes, use `aq home` to select one; that selection refreshes
   its devices and scenes.
5. Type `aq` to browse, or start typing a device, room, or scene name.

macOS may ask you to allow Python to access the Aqara Keychain item. After
replacing or updating Python, you may need to allow access again.

## Commands

Command choices have distinct titles such as “Turn on,” “Turn off,” and
“Set brightness,” with the device, account, and home in the subtitle. A search
matching a single device opens its command list, including when you type only
part of its name.

Tab completes the full device name and command. Commands needing a value keep
a trailing space so you can continue typing. Mode and colour commands suggest
values, and completing a numeric command preserves the value you entered.
For example, `aq Desk dim 50` can resolve a unique `Desk Light` and autocomplete
to `Desk Light dim 50`. Duplicate device matches require selecting the exact
device before a command can run.

Pressing Return on a control sends it to Aqara. Status rows display the device
name as the title and the readings as one subtitle line; selecting a status row
returns to that device's command list. Use `aq showstatus on` to show the summary
above the commands whenever a single device is selected. Names containing
spaces, quotes, or shell punctuation are supported.

| Command | Behavior |
| --- | --- |
| `aq mode` / `aq mode agent` / `aq mode cloud` | View or change the operating mode |
| `aq <search>` | Search device names, rooms, device types, and scenes |
| `aq <device> status` | Read current state (also works for sensors and hubs) |
| `aq <device> on` / `off` | Power controls for lights, switches, outlets, and ACs |
| `aq <light> dim 50` | Brightness, 1–100%; use `off` to turn the light off |
| `aq <light> color blue` | Set a named color, where supported |
| `aq <light> ct 4000` | Color temperature, 2700–6500 K, where supported |
| `aq <curtain> open` / `close` / `stop` | Open, close, or pause a curtain/shade |
| `aq <curtain> slevel 50` | Opening percentage, 0–100 (100 = fully open) |
| `aq <air conditioner> mode cool` | `auto`, `heat`, `cool`, `dry`, or `fan` |
| `aq <air conditioner> temp 24` | Temperature in Celsius, 18–30 |
| `aq scene <search>` | Search saved scenes; Return requests execution |
| `aq update` | Refresh homes, devices, and scenes for the current account |
| `aq home` / `aq homes` | Switch between homes across all saved accounts and regions |
| `aq home <search>` | Filter homes by home name or account label |
| `aq home update` | Discover homes across every saved account |
| `aq account` | List saved accounts and regions; select one to restore its last home |
| `aq account add <name>` | Save another account/region's API key in a secure dialog |
| `aq account rename` | Choose any saved account and enter its new name in a dialog |
| `aq account rename <name>` | Rename the current account profile |
| `aq showstatus on` / `off` | Automatically show live state on device selection; default off |
| `aq login` | Open Aqara's sign-in page |
| `aq apikey` | Replace the current account's API key using a secure dialog |
| `aq reinit` | Reset saved accounts, credentials, home selections, and catalogs for the selected mode |
| `aq workflow:update` | Check GitHub for a newer workflow and start the upgrade |
| `aq workflow:autoupdate` | Enable automatic daily update checks (default) |
| `aq workflow:noautoupdate` | Disable automatic checks; manual updates remain available |

If a device has the same name as a workflow command, use `aq device <name>`.
Devices with duplicate names include room labels; select the exact result to
choose a particular device. Multi-channel products may appear as several
separately controllable devices.

## Multiple homes, accounts, and regions

`aq home` lists every discovered home across all saved accounts. The current
home is marked, and each result includes its account label. Select a home to
switch; the workflow remembers that choice. The first result under `aq` shows
your current account and home and opens the home picker.

All homes exposed by one account/region share its API key. To connect another
account or region:

1. Optionally name the existing connection with `aq account rename Personal`.
2. Run `aq login` and sign in to Aqara Agent with the other account, choosing
   the region that matches its homes in Aqara Home. Copy its `aqara_api_key`.
3. Run `aq account add US` (or any name you prefer) and paste the key in the
   secure dialog. The workflow validates it and discovers all homes it exposes.
4. Use `aq home` to choose among homes from both connections. For example,
   `aq home US` filters by the saved account label.

Each account keeps its own credential, remembered home, device and scene
catalogs, and status cache. Switching back to a previously loaded home restores
its catalog immediately; use `aq update` to refresh it. A home being selected
for the first time loads its catalog from Aqara. Controls and scenes remain
scoped to the account and home where they were selected.
Homes without saved scenes are supported. If loading a new home's catalog
fails, the previous selection and cached data are restored.

To rename a connection that cannot currently load, use `aq account rename`,
select that account, and enter a new display name. Renaming does not require
switching to that account or contacting Aqara. Account names should contain
only a label such as `Work` or `Personal`; API keys go in the separate secure dialog.

Use `aq home update` to discover newly added homes across all saved accounts.
Replacing a key with `aq apikey` clears only that connection's home selection
and catalogs. Adding another connection preserves the existing connections.

Controls are offered by device family. A specific light may lack color or color
temperature support; Aqara can reject a command the hardware does not support.
Aqara Agent [explicitly excludes door lock lock/unlock commands](https://github.com/aqara/aqara-agent-skills/blob/main/skills/aqara-agent/SKILL.md#out-of-scope).
Door locks therefore show status and an explanation of the unavailable controls.
The current API action table also does not document hub security mode controls.
Switch to `aq mode cloud` for supported lock controls; see the
[Cloud mode guide](README-cloud.md#lock-control). Lock and unlock are never guessed or sent through the
generic Aqara device control API. Serialized device status is displayed as
readable fields joined in the device's subtitle, for example:
`Door: Closed · Lock: Locked · Connection: Online`. Internal remote-control flags
are hidden. Copy or Large Type on a status row shows the full summary when the
subtitle is too long to fit.
Notifications describe the requested action, such as “Turning Desk Light
on” or “Setting Desk Light to 50% brightness.” Use `status` to check the
device's resulting state.
Commands are never retried automatically after a timeout, since they may already
have been executed by Aqara.

## Workflow updates

The workflow uses the same bundled Alfred-Workflow updater as
[alfred-hubitat](https://github.com/schwark/alfred-hubitat). It checks GitHub
Releases in the background once a day while you use `aq`. When a newer stable
release is available, an update row appears above the search results. Select
it, or run `aq workflow:update` (`aq upgrade` also works). The workflow downloads
the release, verifies its SHA-256 checksum, bundle identity, and version, then
opens Alfred's import dialog. Confirm the upgrade there to finish installing.
Releases requiring a newer Alfred version are skipped.

Use `aq workflow:noautoupdate` to turn off automatic checks, and
`aq workflow:autoupdate` to turn them back on. `aq update` continues to refresh
your Aqara devices, homes, and scenes. Upgrades preserve the selected mode, both modes' saved accounts, home
selections, and Keychain credentials. Update checks use GitHub's public API;
they do not send Aqara credentials or account data. Updater settings, cached
release information, downloads, and logs live under `workflow-updates/` in the
workflow data directory, separate from each account's settings.

## Local data

API keys are generic passwords in macOS Keychain with service
`com.schwark.aqara`. The original connection uses account `api-key`; added
connections use `api-key:<profile-id>`. Home selections and catalogs are
stored in Alfred's workflow data directory, outside the installed workflow:

```text
~/Library/Application Support/Alfred/Workflow Data/com.schwark.aqara/
```

Search uses the local catalog, so typing does not repeatedly query the API.
The original connection retains its existing files. Added connections have
separate data directories under `profiles/<profile-id>/`; `accounts.json`
stores their labels and the active profile, with no credentials.
Run `aq update` when adding, removing, or renaming devices or scenes. Status
requests are cached for five seconds. Controls, scene requests, home changes,
and reconnection invalidate status. Replacing the key clears the previous
connection's catalogs and home selection. A failed catalog refresh keeps the prior
catalog unless the selected home is no longer available.

If `aq update` reports **NoHomesUnderAccount**, Aqara Agent is not returning any
homes for the saved credential. Check that the M3 appears in Aqara Home under the
same account and region used when signing in to Aqara Agent. Then use `aq login`
and `aq apikey` to reconnect. Workflow errors identify the failed operation and
known account problems without displaying raw server messages or credentials.
Errors appear in Alfred's workflow debugger as well as its notifications.

## Cloud mode

`aq mode cloud` selects the Aqara Home cloud connection in this same workflow.
Cloud unlock followed by lock has been verified with U100 and U300 through the
shared API. Cloud mode retains Agent fallback for commands outside its native
coverage. Switch back at any time with `aq mode agent`.

See [README-cloud.md](README-cloud.md) for setup, command coverage, multiple
accounts and homes, migration from the earlier parallel workflow, and limitations.
The [API research notes](docs/aqara-mobile-api-research.md) describe the underlying
app protocol. Both modes share one package and update channel.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 workflow-build
```

The workflow runner finds Python in the standard Apple Silicon Homebrew,
Intel Homebrew/python.org, and system locations without depending on a shell
profile. The build uses an explicit file allowlist and excludes local data,
tests, `.git`, and credentials. Both macOS's Python 3.9 and current Homebrew
Python are supported.

`filter.py` renders Alfred results, `command.py` executes selected actions,
`common.py` manages Agent requests and the catalog, `cloud_backend.py` adds the
Cloud connection, `runtime.py` persists the mode, and `keychain.py` uses the
native Security framework. API requests are fixed to the official HTTPS host;
redirects are rejected so the bearer key is not forwarded elsewhere.

`workflow/` vendors the Python 3 Alfred-Workflow port bundled with Hubitat
(Alpynist 2.0.0), originally by Dean Jackson and ported by Arthur Pinheiro.
Its MIT license is in `workflow/LICENSE`. Local changes preserve the active
Python interpreter and isolated updater directories in background jobs, skip
draft releases, and verify GitHub asset checksums and workflow metadata before
opening an update. The port's lock files are also closed explicitly for current
Python compatibility. No external Python packages are needed.

## API references

The workflow icon is Aqara's logo, sourced from its
[official GitHub organization](https://github.com/aqara).

- [Aqara's official Python API client](https://github.com/aqara/aqara-agent-skills/blob/main/skills/aqara-agent/scripts/aqara_open_api.py)
- [Aqara's device control action table](https://github.com/aqara/aqara-agent-skills/blob/main/skills/aqara-agent/assets/device_control_action_table.csv)
- [Aqara Agent sign-in and remote API documentation](https://github.com/aqara/aqara-mcp-server)
- [Alfred Script Filter JSON](https://www.alfredapp.com/help/workflows/inputs/script-filter/json/)
