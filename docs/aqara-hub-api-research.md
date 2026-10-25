# M3 hub control research

## Objective and correction

The primary target is **Alfred → Aqara regional cloud → M3 hub → devices**,
with discovery and commands shared across device families and remote homes.
LANLink is an optional local transport. Neither same-LAN access nor direct
phone-to-lock Bluetooth enrollment is assumed to be a prerequisite for the
cloud integration.

The U100's encrypted app-plugin branch establishes requirements for that
branch. It does **not** establish that every hub-level API needs the phone's
lock-pairing key. Likewise, a failed `/matter/dev/detail` call against a Zigbee
lock does not rule out hub LAN control or the hub's Matter bridge. The previous
mobile experiment implemented only part of the available control surface.

## Cloud control is a separate path

The current public [AIOT API guide](https://docs.aqara.com/docs/aqara-developer/aiot-api/getting-started-with-the-aiot-api/)
documents regional HTTPS bases under `{host}/v4.0/open/api`, with developer
application signing credentials (`Appid`, `Keyid`, signature) and account
authorization via `Accesstoken`. These are a separate credential family from
the Agent API key and the mobile account session.

Relevant documented operations are:

- GET `/devices/definitions` for per-device capabilities.
- POST `/devices/trait/execute` for trait-based controls.
- POST `/devices/command/execute` for commands discovered in device definitions.

The [command API](https://docs.aqara.com/docs/aqara-developer/aiot-api/execute-device-command/)
requires endpoint/function/command fields from device definitions and can
report failed items even when the top-level request succeeds. Generic cloud
control availability does not guarantee U100 lock/unlock authorization.

The recovered app independently has a cloud implementation:

```text
DeviceControlFactory strategies
  → LANDeviceControl
  → AqaraDeviceControl
      → DeviceCardDataNetRepository
          → CardDataApi (regional cloud HTTPS)
```

The strategy list also includes a local database implementation. Some factory
methods were not fully decompiled, so the list is not proof of every fallback
or retry behavior. `AqaraDeviceControl.write` and `cmd` do call the network
repository, and `CardDataApi` declares these cloud routes under `/app/v1.0/lumi`:

| Operation | Method/path | App-built body |
| --- | --- | --- |
| Read traits | POST `/app/qlink/trait/read` | `devices`, `needParam` |
| Write one device's traits | POST `/app/qlink/trait/write` | `deviceId`, `traits:[{path,value}]` |
| Write traits in a batch | POST `/app/qlink/batch/trait/write` | `devices:[{deviceId,traits}]` |
| Execute device commands | POST `/matter/write` | `did`, `data:{path:value}` |
| Execute commands in a batch | POST `/matter/write/batch` | `list:[{did,data}]` |

`DeviceCardDataNetRepository` confirms the generic request construction. The
cloud target is a device ID; neither the M3's LAN address nor a LANLink tunnel
appears in those requests. Exact routing and lock authorization remain server
responsibilities to validate. This is not a demonstrated cloud tunnel for
arbitrary LANLink messages. A separate `/dev/rpc` string in a legacy gateway
API class remains only a lead; M3 compatibility and request semantics were not
established.

For this workflow, cloud capability discovery and hub-executed operations take
priority. The U100's inspected `RemoteControlWithAuthCode` branch can require
phone-held material, but it is not the only cloud route: subsequent authorized
tests of the standard command mapping succeeded for both U100 and U300. One
Unlock followed by one Lock was accepted for each tested model, with matching
status readback. The U100 was associated with an M3;
the test used no phone-held pairing key and required no LAN connection.

## Independent evidence

- [Aqara's M3 developer documentation](https://opendoc.aqara.com/en/docs/AqaraDevice/Hub-M3.html)
  documents Matter Controller, Matter Bridge, HomeKit Bridge, scene sync, and
  advanced bridging as separate capabilities.
- [Aqara's LAN-control announcement](https://www.aqara.cn/1934819440644755457)
  describes app control on the same network as an M3, with hub capabilities and
  LAN functionality enabled in app settings. It does not establish that every
  device operation, including U100 unlock, is supported over LAN.
- The official APK's recovered `AqaraLANManager`, `LanDeviceManager`, and
  `LanFunctionImpl` implement hub sessions and generic child-device reads/writes.
- [Aqara-LANLink](https://github.com/absent42/Aqara-LANLink) is an independent,
  MIT-licensed implementation reporting tested M3 connections. The inspected
  revision is `7ddd5314019d8fde8d29d090d93165f3196c7df7`. This is reference code,
  not a verified successful session on this installation.

## Native app's LAN session

Discovery uses `_aqara-setup._tcp.local.`. The recovered app selects LAN-control
candidates with TXT fields `pv=true`, `ac=2`, and bit 5 set in decimal `ca`.
The `id` field supplies the hub DID, `md` its model; DNS-SD supplies host and
port. The advertised port must be resolved rather than guessed or hardcoded.

The native flow is:

```text
DNS-SD discovery
  → AqaraChannelManager TCP connection
  → com.aqara.lanlink.Tunnel Start/Talk
  → encrypted tunnel ready
  → session checkin
  → topology, reads, writes, scene execution
```

`Tunnel` loads `liblanlink.so`. Its JNI entry points are `Start`, `Stop`,
`Talk`, `Incoming`, and `Outgoing`. The library exports ECDH and AES helpers.
The independent reference reconstructs the ECDH exchange, AES-128-CBC tunnel,
framing, and session messages. Raw JSON is not a substitute for that transport.

App-code check-in envelope:

```json
{"seq":1,"type":"session","cmd":"checkin","data":{"user":"<user-id>","did":"<hub-did>","aid":"<token>"}}
```

The app's `startLAN(context, userId, token)` supplies the session fields.
The reference uses a mobile account session token here. This is distinct from
an Aqara Agent API key and from a phone's lock-specific pairing key. Successful
local authentication still needs testing with the chosen hub and account.
The app checks the check-in result's `data.code` for zero before marking LAN ready.

The hub pushes topology data that maps hubs to reachable child IDs, as well as
events identifying a Matter broker. `supportLanControl(parentDeviceId, did)`
uses this topology, rather than a separate transport implementation per model.

## Generic hub command envelopes

Recovered child-device read construction:

```json
{"seq":2,"type":"device","cmd":"read","data":{"did":"<parent-hub-did>","sdid":"<child-did>","model":"<model>","value":["<trait-path>"]}}
```

Write construction uses `cmd=write`, a path/value object in `data.value`, and
a source field. Scene construction uses `cmd=scene` with a `sceneId` array.
One app class uses `src` in the scene payload and another uses `source`; exact
version-specific behavior must be checked, not silently conflated. The `model`
argument is passed through by the app; the reference documents hub-model
framing for relayed requests. Verify this against actual app traffic before
controlling devices.

LANLink/QLINK trait paths have three components, such as
`<endpoint>.<function>.<trait>`. They must not be confused with the four-component
resource IDs previously used with `/res/query/by/resourceId`, or with Matter
command argument paths ending in `.0`.

## U100 leads and limits

The independent reference includes U100 (`aqara.lock.acn10`) endpoint/trait data
for LockState, DoorState, EnableRemoteControl, SupportedRemoteFeatures,
RemoteUnlock, and RemoteControlWithAuthCode. That catalogue is a lead for
introspection, not proof that a particular write unlocks the lock.

Recovered native `DoorLockFunc` and `DoorLockViewModel` distinguish three paths:

1. `RemoteControlWithAuthCode`, built by `DoorLockUtil` using the phone's stored
   pairing material.
2. Matter LockDoor/UnlockDoor commands.
3. A `RemoteUnlock` trait write when the other routes are absent.

The inspected selection prioritizes the authorization-code path when present.
Therefore the existence of `RemoteUnlock` alone does not establish that it is
an accepted, key-free override. Boolean operation direction must be established
from the call site and device metadata; do not guess it from the trait name.
The hub's own Matter-bridged lock endpoint is another distinct candidate.

Subsequent tests answered the primary cloud question: the U100's declared
LockDoor/UnlockDoor commands work through the shared `/matter/write` route
while associated with the M3, without copying a phone key. This does not
establish support for the separate encrypted branch or every LAN operation.

## Additional read-only discovery

The research client now supports:

| Method/path under `/app/v1.0/lumi` | Purpose |
| --- | --- |
| GET `/app/layout/collection/panels` | Device endpoint panels, with `subjectIds=<did>` and `types=device_endpoint_panel` |
| POST `/app/qlink/trait/read` | Explicit three-part paths with `needParam=true` and `needSubscribe=false` |

The panel response reference describes `result[*].subjects[*]` with endpoint
metadata and paths. This provides a non-Matter introspection route for Zigbee
devices. No writable control is inferred solely from a global specification.

`tools/hub_probe.py` performs DNS-SD discovery and optionally an encrypted
handshake/check-in against an explicitly selected hub. It transmits no device
writes, scene runs, pairing changes, or lock operations. By default the
handshake sends no account credentials. `--profile` explicitly loads a saved
mobile session from Keychain for check-in. Output summarizes success without
printing device IDs, addresses, tokens, or topology contents.

The probe imports only the separately cloned reference's reviewed `crypto.py`,
`ecc.py`, and `tunnel.py`, verified by SHA256. It requires the research runtime's
`cryptography` package; production workflow dependencies are unchanged. Its
M3 adapter rejects unknown verification-proof layouts instead of adopting the
reference's permissive compatibility fallback. LANLink's unauthenticated ECDH
does not establish a hub's cryptographic identity; the destination must be the
owner's selected hub on a trusted network.

Example discovery, saving private addresses outside the checkout:

```sh
python3 tools/hub_probe.py --discover \
  --output "$HOME/.cache/alfred-aqara/hub-discovery.json"
```

For a handshake, pass a selected `{host,port,did}` object on stdin to the probe
with `--reference <separately-cloned-Aqara-LANLink-directory>`. Add
`--profile <saved-mobile-profile-id>` only for the selected account's check-in.
Raw reference code, proprietary native libraries, captures, and private
discovery records are excluded from the workflow and repository.

## Cloud implementation and validation

The experimental runtime now uses the shared collection-panel and trait-read
routes for endpoint discovery, alongside Matter details. It binds a response
to the requested device and exact endpoint. A unique returned `subjectKey`
can associate a native endpoint with an Agent endpoint; names cannot.
Matched endpoints retain the full Agent command set. Common native power,
brightness, and color-temperature writes use `/app/qlink/trait/write`, with
fresh capability checks and no retry through another transport. Unknown
traits are not written. The presence of an authenticated lock-control trait
does not invalidate separately declared standard commands; the live U100
test established that these routes can coexist.

`tools/cloud_probe.py --devices` gathers private read-only evidence from all
connected regional accounts and homes. It includes parent-device metadata,
panels, trait parameters, Matter details, and the global specification. It
does not send scene runs, lock commands, or trait writes. Evidence is confined
to the private Aqara cache with mode `0600`; the tool and evidence are excluded
from workflow packages. Offline tests establish parsing and request behavior;
the separate authorized live command tests establish cloud acceptance and
reported lock-state transitions. Human observation of mechanical movement was
not part of those API tests.

The runtime downloads Aqara's current gzip endpoint definitions from
GET `/app/qlink/specf/config/url`, validates the advertised MD5 and size,
and reads the `configs` list as data. An inventory model selects its endpoint
definition; a live LockState read must confirm the device before definition
commands become available. This avoids bundled per-model drivers and covers
Zigbee devices whose `/matter/dev/detail` response is unavailable. The standard
LockDoor/UnlockDoor command paths then use the same `/matter/write` body as
native Matter locks. Execution rechecks the device's home/model and endpoint.

Public developer API coverage and mobile private API coverage must be tested
separately; their credentials and permissions are not interchangeable.

### Cloud bridge discovery

Recovered `IDeviceHttpApi` and `DeviceHttpModel` also expose these read-only
queries. Request field names were checked with constant replacement disabled
in the decompiler, and the two bridge POSTs call query methods rather than
the adjacent bind/unbind methods:

| Query | Method / path below `/app/v1.0/lumi` | Fields |
| --- | --- | --- |
| Gateway detail | GET `/dev/query/gateway/detail` | Query `deviceId` |
| Gateways eligible to bridge a device | POST `/app/dev/matter/bridge` | `deviceId`, `positionId` |
| A gateway's bridge subdevices | POST `/app/dev/matter/bridge/subdevice` | `positionId`, `gatewayId` |

`MatterBridgeGatewayBean` contains `deviceId`, model/name/position fields,
online/support/SSID-support states, and current/maximum device counts.
`MatterBridgeSubDeviceBean` adds a bind-support state. Neither response type
contains a Matter endpoint ID or a lock command definition. Bridge eligibility
therefore does not establish an executable cloud lock route.

`tools/cloud_probe.py --bridges` includes these queries, following gateway IDs
returned by parent relationships and bridge metadata. It also reads each
identified hub's Matter details, so a hub-owned lock endpoint can be checked
without assuming the U100 itself supports `/matter/dev/detail`. No bridge
binding, unbinding, or device reconfiguration is performed. Live queries
returned an empty gateway-detail result, code `9999` for the gateway-eligibility
query, and bridge-subdevice support metadata. These results did not expose
a usable Matter lock endpoint. Standard cloud command execution nevertheless
succeeded, so bridge eligibility and `/matter/dev/detail` must not be used
as the sole tests of cloud control support.

## Optional LAN validation

1. Resolve a reachable M3 advertisement with Hub Cluster/LAN Control enabled.
2. Establish the encrypted hub session and authenticate the owning account.
3. Read hub topology and per-device endpoint metadata, including the U100.
4. Determine the supported hub-owned lock action, including whether the hub's
   Matter bridge exposes it and what permissions it requires.
5. Test a chosen lock operation with explicit physical-test authorization;
   verify reported state and actual movement before enabling it in Alfred.

Brief local discovery is not proof of absence, and discovery of Matter/HomeKit
services is not proof that the specific M3 or its U100 endpoint was found.
LAN control requires network reachability; homes on another network require
an appropriate private network path or a separately verified remote transport.
The existing Agent functionality remains the baseline during this experiment.
