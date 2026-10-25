# Aqara mobile API reference and research notes

Investigated 2026-10-24 for the Alfred Aqara workflow, following the local
`scraper-tools:discover-mobile-api` skill. This report contains technical
protocol findings only; account details, device identifiers, credentials,
session tokens, and returned device states are omitted.
All example identifiers and credentials below are placeholders. Observations
describe the investigation date, not a supported or stable public contract.

Contents:

- [Findings and evidence levels](#finding)
- [Evidence and sources](#evidence-and-sources)
- [Authentication, regions, and signing](#authentication-and-read-only-queries)
- [Verified read requests and capability catalog](#verified-read-request-reference)
- [U100 encrypted control and pairing keys](#u100-encrypted-remote-control-branch)
- [U300 Matter control and authorization](#u300-matter-capabilities-and-native-authorization)
- [Other route leads and failed experiments](#additional-route-leads-and-inconclusive-results)
- [Probe usage, validation, and remaining work](#probe-validation-and-remaining-work)

## Finding

Mobile-account authentication, home/device discovery, and lock-state reads
are now verified against the service. The U100 and U300 app plugins expose
different control paths. Neither lock nor unlock has been sent or verified.
The released and installed Alfred workflow still uses Aqara Agent; this
research does not add mobile lock control to the workflow.

The U100's Zigbee remote-control branch encrypts a command with a phone-stored
pairing key. The U300's Matter branch delegates control to the app's native
`AHDoorLock.controlLockState` module. Its advertised commands are confirmed,
but the native module's final authorization and request handling remain
untraced. An HTTP success, advertised capability, and observed physical lock
movement are separate checks.

### Evidence levels

| Level | Meaning | Examples |
| --- | --- | --- |
| Live-tested | A signed request succeeded against the service | Login, home/device lists, U100 and U300 status reads |
| App-code finding | Read directly from the official APK or a model plugin | Native U300 control bridge, U100 encryption and pairing-key setup |
| Community claim | Described by the pinned independent reference, not validated here for these models | D100 cloud control, BLE handshake response contents, local P2P transport |
| Unresolved | No working implementation or conclusive runtime trace yet | U100 remote-key acquisition outside the phone; U300 native authorization; actual lock movement |

### Relationship to the existing workflow

The Agent API and mobile API are separate interfaces:

| Detail | Existing Aqara Agent integration | Mobile API investigated here |
| --- | --- | --- |
| Authentication | Agent bearer key | Aqara Home account login, followed by signed requests with a session token |
| Home selection | Agent home context, including the `position_id` header | Home/device discovery and explicit `positionId` or `did` parameters |
| Device identity | Agent endpoint IDs and generic device families | Mobile `did`, hardware `model`, and sometimes Matter metadata |
| Lock support established here | Status | Status plus control-path discovery; working lock/unlock remains unverified |

An Agent key cannot be substituted for a mobile session. An Agent device
type such as `DoorLock` also does not identify the mobile hardware model or
prove that any particular command is supported. A future integration must
establish a mapping between those identities rather than matching by name
alone. Keep account sessions and selected homes separate: a home switch
within an account should not accidentally reuse a different account's
session, region, or device catalog.

## Evidence and sources

The international Android APK came from Aqara's own
[download page](https://cdn.aqara.com/cdn/luoshu/mainland/static/app-download/index.html),
which links [aqara-home.apk](https://cdn.aqara.com/cdn/luoshu/mainland/static/app/aqara-home.apk).
Static analysis used JADX 1.5.6 and APKTool 3.0.3.

- Package: `com.lumiunited.aqarahome.play`.
- Version: 6.4.0 (7011), as served during inspection.
- Size: 264,622,915 bytes.
- SHA-256: `518e75081778234f193926450f03b602297a5fa1523a92513b4bb5cb36267bd4`.
- Most Java implementations are behind SecNeo components, including
  `com.secneo.apkwrapper`, `libDexHelper`, `libdexjni`, and `libdatajar`.
- The base React Native bundle contains Hermes bytecode. The separately
  downloaded model plugins contain readable JavaScript.

The mobile plugin catalog identifies these model-specific packages:

| Model | App model ID | Plugin version | Official download |
| --- | --- | --- | --- |
| U100 | `aqara.lock.acn10` | 1.0.484 | [Plugin archive](https://cdn.aqara.com/cdn/appadmin/mainland/rn/2adc1b53e6934655a69e5c8b30409056.zip) |
| U300 | `aqara.matter.4447_10241` | 1.0.496 | [Plugin archive](https://cdn.aqara.com/cdn/appadmin/mainland/rn/4b91ea5820164459816f59dedbcd06bd.zip) |

Archive SHA-256 values:

- U100: `4f625c2293ba64f3ac6b6696d9c9372251e938fd79961b306ef25823db6c8151`.
- U300: `e88959678e097c1f6777c69bc8844e6b3a334944ae00704508d454b75560c8aa`.

The shared plugin source paths below identify the inspected modules. The
proprietary archives and extracted source are not redistributed here.

## Authentication and read-only queries

All routes below use the regional host plus `/app/v1.0/lumi`.

| Method | Route | Verification |
| --- | --- | --- |
| POST | `/user/guard-code/login` | Authenticated session obtained |
| GET | `/app/position/query/home/list` | Home discovery succeeded |
| GET | `/app/position/device/query` | Device/model discovery succeeded |
| GET | `/app/dev/query/detail` | Model details returned |
| GET | `/app/rn/latest/list` | Matching official model plugins located |
| POST | `/res/query` | U100 lock-state resources returned |
| GET | `/matter/dev/detail` | U300 endpoint/function/trait metadata returned |
| POST | `/res/query/by/resourceId` | U300 numeric lock-state resource returned |

The wire-format starting point was the MIT-licensed community project
[aquara-local](https://github.com/home-assistant-plugins/aquara-local),
commit `4fc274688cd40774307549172c9ae1db073c92ca`, which is unaffiliated with
Aqara or Home Assistant. Its
[API reference](https://github.com/home-assistant-plugins/aquara-local/blob/4fc274688cd40774307549172c9ae1db073c92ca/docs/API.md)
describes password encryption and signed requests. These formats have now
been validated for authentication and the reads above:

- Password: MD5 hex text, encrypted with RSA PKCS#1 v1.5, then Base64 encoded.
- Signature: MD5 of the app ID, nonce, timestamp, optional nonempty session
  token, request payload, and shared app key, in the expected order.
- GET parameters are signed before URL encoding; POST parameters use compact
  JSON. Field order is preserved.
- Mobile sessions are separate from Aqara Agent bearer keys.

The reference's US hostname returned HTTP 500. Aqara's official
[Android SDK region table](https://opendoc.aqara.com/docs/SDK开发/Android开发指南/地区对照表.html)
and host strings in the APK identify `https://aiot-rpc-usa.aqara.com`.
Authentication succeeds with that hostname. This is a generic routing fix,
not an account-specific workaround.

The reference app identity reports wire version 6.1.6, distinct from the
inspected APK version 6.4.0. Successful reads validate that identity for those
requests, without establishing control compatibility or future availability.

### Regional routing and transport

The probe uses this fixed hostname map. Only the SEA and US entries were
validated by authenticated requests during this investigation; the others
remain reference-derived entries.

| API `area` | Host |
| --- | --- |
| `SEA` | `https://rpc-au.aqara.com` |
| `US` | `https://aiot-rpc-usa.aqara.com` |
| `EU` | `https://rpc-ger.aqara.com` |
| `KR` | `https://rpc-kr.aqara.com` |
| `CN` | `https://aiot-rpc.ankasa.cn` |

`area` selects the backend. Login `district` is a separate uppercase,
two-letter country code. The probe requires both explicitly and does not
try credentials against a succession of regions. The host alone is not a
reliable country selector. The reference's CN handling uses district `CN`,
but that region's login was not tested here.

Use HTTPS and the `/app/v1.0/lumi` prefix. POST requests in the verified
flow carry ordinary JSON; no additional `x-aes128gcm` envelope was needed.
Password-field RSA encryption is still required. HTTP/2 is used by the
Android transport according to the community reference, but a
standard-library HTTP client also succeeded, so HTTP/2 was not a
prerequisite for the verified reads.

### App identity and password encoding

The starting reference supplies `APPID`, `APPKEY`, `RSA_N`, and `RSA_E`
in `custom_components/aquara_local/const.py`. These are app constants,
distinct from an account password, session token, or lock-pairing key.
Their values are deliberately not copied into this document or repository.
The loader in [mobile_api.py](../tools/mobile_api.py) parses literal
assignments using Python's AST and never imports or executes that file.

The password-field transformation is:

```text
password_utf8 = UTF8(account_password)
digest_text = ASCII(lowercase_hex(MD5(password_utf8)))
ciphertext = RSA_PKCS1_v1_5_encrypt(app_public_key, digest_text)
password_field = Base64(ciphertext)
```

The RSA exponent is `65537`. The implementation uses fresh nonzero random
PKCS#1 padding for every encryption; ciphertext for the same password is
therefore not deterministic. Do not encrypt the raw password or the 16 raw
MD5 digest bytes: the input is the 32-byte hexadecimal ASCII representation.

Login uses POST `/user/guard-code/login`. Its body fields, in the order
used by the probe, are:

```json
{"account":"<account-login>","district":"<country-code>","encryptType":2,"guardCode":"","password":"<rsa-base64>"}
```

An empty `guardCode` worked for the tested login flow. This does not
establish that every account, risk challenge, or MFA configuration can
authenticate without an additional code.

### Request headers and exact signature format

The successful probe sends these headers on each request:

| Header | Value or source |
| --- | --- |
| `appid` | App identity |
| `token` | Session token; empty on login |
| `userid` | Session account identifier; empty on login |
| `nonce` | Fresh 16 random bytes represented as 32 uppercase hexadecimal characters |
| `time` | Decimal Unix epoch milliseconds as a string |
| `area` | Selected API area |
| `sign` | Lowercase MD5 hexadecimal signature described below |
| `lang` | `en` |
| `app-version` | `6.1.6`, the reference wire identity |
| `phone-model` | `Android` |
| `sys-type` | `1` |
| `sys-version` | `14` |
| `content-type` | `application/json; charset=utf-8` |

This is a custom signing scheme, not HTTP Bearer authentication, HMAC, or
an OAuth flow. Header-name casing is not the same issue as the capitalized
field names inside the signature input. The exact input is:

```text
input = "Appid=" + app_id + "&Nonce=" + nonce + "&Time=" + time
if token is nonempty:
    input += "&Token=" + token
if payload is nonempty:
    input += "&" + payload
input += "&" + app_key
sign = lowercase_hex(MD5(UTF8(input)))
```

Omit the entire `&Token=...` segment for login; signing an empty token
segment changes the digest. Do not log this input because it includes the
shared app key and, after login, a private session token.

For POST, `payload` is the exact compact JSON body sent over the network.
The probe uses `json.dumps(fields, separators=(",", ":"), ensure_ascii=True)`
and sends the UTF-8 encoding of that same string. Serializing again with
different whitespace, escaping, or field ordering can invalidate a signature.
The successful experiments do not establish that fields must be sorted;
the implementation preserves insertion order.

For GET, concatenate fields in order as raw `key=value` pairs separated
by `&`, sign that unescaped string, then URL-encode the fields for transport.
For example, a device-detail query signs a JSON array in the raw `dids`
value; its brackets and quotes are percent-encoded only in the URL.
Do not sign the already encoded URL. Reuse exactly the signed nonce and
timestamp in the corresponding headers.

### Response envelope and session handling

The probe expects an object with numeric `code: 0`, then unwraps `result`,
or `data` if the `result` key is absent. The unwrapped payload can be a
list or object; it is not necessarily the same shape across endpoints.
An empty successful list is distinct from an API error and from evidence
that a device lacks a feature.

Login must return a nonempty token and a user identifier. The probe accepts
the identifier at `result.userId` or `result.userInfo.userId`, normalizing
it to a string for the header. A new login clears the old in-memory session
before making the request; failed login does not retain the previous token.

Token lifetime, refresh/revocation endpoints, simultaneous-session behavior,
rate limits, and lockout thresholds have not been established. There is no
automatic retry, region probing, token refresh, or saved session in the
experimental client.

## Verified read request reference

Examples below show field names and shapes only. `<home-id>`, `<did>`, and
`<endpoint>` must come from the appropriate authenticated discovery response.
The signed-query order shown matches the probe. A literal `<endpoint>` is
not a valid numeric resource path.

### Homes and devices

```text
GET /app/position/query/home/list
needDefaultRoom=false&size=300&startIndex=0

GET /app/position/device/query
positionId=<home-id>&size=300&startIndex=0

GET /app/dev/query/detail
area=<area>&dids=["<did>"]
```

The first two queries expose `startIndex` and `size`. The probe requests
300 entries and accepts an explicit starting index; traversal beyond the
first page was not validated. Do not assume one home or one page per account.
The reference describes a `homes` collection with `homeId` and `homeName`;
these should be treated as service response fields, not fixed personal
configuration. Device detail's `dids` is JSON text inside a GET parameter,
not repeated query keys. Boolean query values such as `false` are lowercase
wire strings, not Python's `False` spelling.

Device-list entries observed during discovery contain these field names:

| Purpose | Fields |
| --- | --- |
| Identity and routing | `did`, `parentDeviceId`, `positionId` |
| Display labels | `deviceName`, `originalName`, `positionName`, `categoryNames` |
| Model/type | `model`, `devicetype`, `type`, `usageType`, `layout` |
| Additional metadata | `firmwareVersion`, `createTime`, `lowBatteryFlag`, `pwdState`, `state`, `supportHomeKit` |

Field availability can vary; no universal schema or semantics for all
these fields were established. Device detail additionally exposed fields
such as `modelName` and `modelType`. Names, IDs, and raw records are private
and are not retained in this reference.

A model-name filter containing only the word `lock` misses native Matter
locks. In particular, U300 uses `aqara.matter.4447_10241`. Use a confirmed
model mapping or returned DoorLock capabilities rather than a device's
display name or that substring test.

### U100 named resources

POST `/res/query`:

```json
{"data":[{"subjectId":"<did>","options":["batt_0_remain_percentage","lock_state","device_offline_status"]}]}
```

This returned the requested named resources for U100. The reference's
resource rows use fields such as `attr`, `value`, `timeStamp`, and `subjectId`.
Do not assume resource values arrive as numbers instead of strings, or
interpret a missing row as an unlocked state. This named-resource query
returned an empty result for U300 in the experiment; its numeric path read
below succeeded instead.

### U300 numeric resources and capability discovery

```text
GET /matter/dev/detail
did=<did>&fullValue=true
```

The successful detail response exposed top-level `positionId`, `devices`,
`nid`, `userId`, and `centralGateway`. The relevant capability hierarchy is:

```text
devices[]
  did
  eps[]
    id            endpoint number
    type
    funcs[]
      id          function number
      traits[]    entries include id and args
      cmds[]      entries include id
```

Find the intended device and the DoorLock function `148`, then derive
the endpoint and supported traits/commands from its metadata. A generic
schema entry elsewhere in the catalog is not equivalent to an entry here.

POST `/res/query/by/resourceId`:

```json
{"data":[{"subjectId":"<did>","options":["<endpoint>.148.32970.1"]}]}
```

The successful response was a list of resource rows with keys
`timeStamp`, `resourceId`, `oldValue`, `value`, and `subjectId`. Actual
identifiers and values are omitted. The suffix `.1` comes from the plugin's
`SpecUtil.getTraitSubscriptionPath`; omitting it produced an empty list.
Read resource IDs and command IDs use different final components.

### Model plugins and the global specification

```text
GET /app/rn/latest/list
model=<model-id>

GET /app/aqara/spec/config
model=<model-id>
```

Both were successful exploratory reads. The plugin endpoint returned a
broader list even with a `model` parameter; select the exact matching model
record before using its plugin version and archive URL. The two matching
archives listed above could be downloaded from Aqara's CDN without adding
account authentication headers. Do not forward a session token or signed
API headers to an arbitrary download URL.

The specification response has `funcList` and `typeToFuncs`. It is a global
catalog, not the device's advertised capability list. This route was used
only in the exploratory session; it is not in the committed probe's fixed
allowlist. Plugin lookup is in that allowlist.

### DoorLock specification values

The global function `148` is named `DoorLock`. Relevant catalog definitions
are listed below; support still requires checking model/device metadata.

| Trait ID | Catalog name | Catalog values, where defined |
| --- | --- | --- |
| `32970` | `LockState` | `0` NotFullyLocked; `1` Locked; `2` Unlocked |
| `32971` | `DoorState` | `0` DoorOpen; `1` DoorClosed; `2` DoorJammed; `3` DoorForcedOpen; `4` DoorUnspecifiedError; `5` DoorAjar |
| `33025` | `EnableRemoteControl` | `0` false; `1` true |
| `33026` | `SupportedRemoteFeatures` | No enum decoded here |
| `33061` | `RemoteUnlock` | `0` false; `1` true |
| `40000` | `RemoteControlWithAuthCode` | No enum decoded here |

In this catalog, `params["6"]` contains a JSON-encoded enum list.
`params["7"]` appears to hold default metadata; that interpretation has
not been independently verified. Neither catalog field is a device's
current state. These numeric-trait definitions must not automatically be
applied to every legacy named resource with a similar name.

| Command ID | Catalog name | Catalog parameter metadata |
| --- | --- | --- |
| `35010` | `LockDoor` | Parameter `0`, labeled authorization code |
| `35011` | `UnlockDoor` | Parameter `0`, labeled unlock key |
| `35020` | `QuietLockDoor` | No parameters listed |
| `35021` | `QuietUnlockDoor` | No parameters listed |
| `40031` | `UnboltDoor` | Parameter `0`, labeled code/encoding |

The parameter labels above paraphrase the catalog's Chinese descriptions.
They do not establish how native authorization populates those parameters,
whether an empty value is accepted, or whether quiet/unbolt commands are
available for U100 or U300. No command in this table was executed.

## U100: encrypted remote-control branch

Inspected shared modules include:

- `Modules_common-lock-config/DeviceConfig/U100.ts`.
- `Modules_common-lock-config/DeviceConfig/FeatureConfig/CommonConfig.ts`.
- `src/Pages/Home/components/UnlockManager.ts`.
- `Modules_common-lock/src/Pages/UserManagement/VistorPwd/RemoteHandler/RemoteHandlerManager.ts`.
- `Modules_common-lock/src/Pages/RemoteGuidePage/RemoteFuncManager.ts`.
- `Modules_common-lock/src/Bluetooth/LockSettingsBleHandler.ts`.
- `Modules_common-lock/src/Http/ResourceHttpHandler.ts`.

The Zigbee remote branch requires the remote function to be enabled and the
hub connection to be online. Its default control mode uses a Bluetooth-format
command relayed over the `e2e_remote_tunnel` resource. The resource writer
posts to `/res/write`, with `subjectId` and a `data` object.

Before writing, the app combines server time, the command, and a CRC16,
encrypts them using its AES-256-CBC helper, then prefixes a key ID. The key
and key ID are read through `AHEncryptUtil.queryKeyChainKeyValue`, using
per-model/per-device names. The inspected setup path generates a key and
provisions it through an authenticated Bluetooth exchange before storing it
locally. Account login alone does not supply those values through this path.

### App-code command construction

The inspected `openDoorLockByRemote(isOpen)` builds a command from system
main command `01`, subcommand `74` (`BLE_OPEN_LOCK`), and one final byte:

| Requested operation | Plain command bytes, in hexadecimal |
| --- | --- |
| Unlock (`isOpen=true`) | `01 74 01` |
| Lock (`isOpen=false`) | `01 74 00` |

These are internal pre-encryption command bytes, not an HTTP request or a
complete BLE packet. They have not been sent to a device during this work.
The inspected transformation is:

```text
command = main-command || subcommand || open-or-close-byte
time-prefix = app_time_encoding(server_epoch_milliseconds)
plaintext = time-prefix || command || app_mijia_crc16(command)
ciphertext = AHEncryptUtil.encryptAES256CBC(pairing-key, plaintext)
resource-value = key-id || ciphertext
```

The timestamp helper separates epoch seconds and the milliseconds remainder,
converts them through the app's hexadecimal-formatting helper, and places
the milliseconds component first. Exact byte order, the CRC helper's full
behavior, and the native AES helper's IV, padding, and representation still
need independent validation before implementing compatible encryption.
The method name alone is insufficient to reconstruct that full encoding.

The resulting app-side resource writer has this shape:

```json
{"data":{"e2e_remote_tunnel":"<key-id-and-encrypted-command>"},"subjectId":"<did>"}
```

It uses POST `/res/write`. This is an app-code finding only; the read-only
client deliberately rejects the route. The shared SDK also has an
`independentResource` branch using `remote_lock_operate_wifi` with different
values and model configuration. Finding that simpler branch in a common
bundle does not establish it as U100's control protocol.

### Pairing-key lifecycle

The app constructs local key names from the model and device ID, with
`uuid`, `randomString`, and `keyid` suffixes. `RemoteFuncManager` first
checks for a stored key. Its inspected provisioning path requires a logged-in
Bluetooth connection when the key is absent. It requests random material
with `generateByteRandomString(32)` and calls `setRemoteKeyID(randomKey, uuid)`.
That BLE setup routine uses main command `01`, subcommand `8a`, and the
random material plus UUID. On success it stores the random material and
returned key ID through the app's keychain helper.

Related code can request an E2E secret-key report via BLE subcommand `f4`
and can post `{deviceId, msg}` to `/app/lock/secret/report`. These are
discovered setup/reporting operations, not demonstrated read-only ways to
retrieve a usable key from a cloud login. None were invoked.

The app also waits for operation callbacks or a resulting state change
after issuing remote control. Returning from the write function alone
should not be presented to an Alfred user as confirmed lock movement.

No pairing key has been extracted, no Bluetooth pairing has been attempted,
and no remote-function setting has been changed. A separate mobile control
route could exist; the inspected branch does not establish one.

The community D100 implementation reports `/matter/write` control with
`pwd` and `type` fields. That report is model-specific and does not validate
the same operation for U100.

## U300: Matter capabilities and native authorization

The relevant shared modules are:

- `Modules_common-lock-config/DeviceConfig/U300.ts`.
- `Modules_common-aqara-spec/src/domain/MatterDomain.ts`.
- `Modules_common-aqara-spec/src/constants/spec.ts`.
- `Modules_common-aqara-spec/src/util/SpecUtil.ts`.
- `Modules_common-lock/src/Pages/Settings/WorkMode/handler/RemoteUnLockPwdHandler.ts`.
- `src/Pages/Home/components/UnlockButtonHelper.ts`.

`Modules_common-lock-config/DeviceConfig/U300.ts` marks U300 as a Matter
device. The shared Matter code uses:

- GET `/matter/dev/detail` with `did` and `fullValue=true`.
- POST `/res/query/by/resourceId` with
  `data: [{subjectId: did, options: [resourcePath]}]`.
- A status resource path of `endpoint.function.trait.1`.
- A command path of `endpoint.function.command.0`.

The trailing `.1` is required for the lock-state query: the shorter path
returned an empty list. The plugin's `SpecUtil.getTraitSubscriptionPath`
confirmed the correct form, and the corrected read returned a value.
The DoorLock function is `148`, lock-state trait `32970`, lock command
`35010`, and unlock command `35011`. The device detail advertises both
commands. Endpoint numbers should come from device metadata rather than
being assumed for every lock model.

`RemoteUnLockPwdHandler.ts` delegates lock/unlock to native
`AHDoorLock.controlLockState`, passing `did`, `model`, `cmdId`, and an empty
JavaScript `value`. This does not prove that the eventual HTTP request has
no additional authorization. The native implementation is protected in the
APK and has not been dynamically traced.

### Native bridge versus cloud writer

The JavaScript bridge passes this conceptual argument object for either
operation:

```json
{"did":"<did>","model":"<model-id>","cmdId":"<endpoint>.148.<command>.0","value":""}
```

`<command>` is `35010` for lock or `35011` for unlock. The plugin constructs
its native bridge paths from shared command specifications. The generic
command spec uses endpoint `2`, but future client logic must reconcile
that against device metadata rather than applying it to arbitrary models.

Separately, the shared Matter writer posts to `/matter/write`, wrapping
its argument as `{did, data}`. The community D100 client adds `pwd: ""`
and `type: 0` and reports physical control for that model. These are three
different pieces of evidence: a native bridge invocation, a shared cloud
writer, and another model's reported implementation. Their combination
does not establish the final U300 wire request or prove that an empty
device password is accepted.

The app's UI also checks that the device is bound to Aqara Matter and that
its Matter connection is online before offering the remote Matter branch.
Neither a `DoorLock` function nor native Matter support alone guarantees
that this condition is satisfied in every installation.

The same module contains a read-only password-state query at
`/devex/device/pwd/state/query` and password-management writes at
`/devex/lock/pwd`. None were called. In particular, its function named
`validateDevicePassword` uses a password-change request; it must not be
treated as a harmless read-only validation endpoint.

The app-code request details are:

| Purpose | Method and path | Body fields |
| --- | --- | --- |
| Read password state | POST `/devex/device/pwd/state/query` | `did` |
| Disable device password | POST `/devex/lock/pwd` | `method: 0`, `did`, `devPwd` |
| Enable device password | POST `/devex/lock/pwd` | `method: 1`, `did`, `newDevPwd` |
| Change device password | POST `/devex/lock/pwd` | `method: 2`, `did`, `devPwd`, `newDevPwd` |

The password-management helpers MD5-hash the device password before sending
it. This device password is distinct from the Aqara account password and
from U100's pairing key. The misleadingly named validation helper sends
method `2` with equal old and new hashes. These findings document the app;
they are not tested administration APIs and are outside the probe allowlist.

## Additional route leads and inconclusive results

The following suffixes were present in `libdatajar.so`. Some original
strings omit the leading slash; they are normalized below. A string match
does not establish an HTTP method, payload, permission, or working feature.

| Candidate route | Evidence and limit |
| --- | --- |
| `/matter/write` | APK string and shared plugin writer; U100/U300 control not tested |
| `/dev/lock/query` | APK string; community reference describes credential metadata; not called |
| `/dev/bluetooth/login/assure/publickey` | APK string; community BLE authentication lead; not called |
| `/dev/bluetooth/login/assure/verify` | APK string; community BLE authentication lead; not called |
| `/devex/lock/p2p/info` | APK string only for the lock-specific variant; not called |
| `/devex/lock/p2p/sign` | APK string only for the lock-specific variant; not called |

The same APK evidence includes the login, home-list, and device-list routes
that were subsequently tested successfully. Exact string offsets and source
hashes are in the local ignored `.cache/analysis-evidence.json`.

The community reference additionally describes POST
`/dev/bluetooth/login/assure/publickey` with `deviceId`, followed by POST
`/dev/bluetooth/login/assure/verify` with `deviceId` and `devicePublicKey`.
It reports cloud public-key/MAC data from the former and session-key,
nonce, and verification data from the latter. No such handshake, returned
key material, or compatibility with U100/U300 was validated here.

Its local P2P examples use `/devex/camera/p2p/info` and
`/devex/camera/p2p/sign`; those camera paths must not be silently equated
with the APK's lock-specific paths. The reference's LAN-control descriptions
also conflict across documents. Local hub/P2P control remains an unverified
lead rather than an alternative known to work.

Shared plugin code contains additional Matter binding, removal, fabric,
and user/credential-management helpers. Their presence in a common SDK is
not a reason to exercise them during lock-state discovery. Credential
enumeration, pairing changes, password changes, and device removal were
not performed.

### Observed failures and interpretation

| Experiment | Observed result | What was learned |
| --- | --- | --- |
| US login using the community hostname `rpc-us.aqara.com` | HTTP 500 | Correcting to the SDK/APK host `aiot-rpc-usa.aqara.com` allowed authentication; this failure was not sufficient evidence of bad credentials |
| GET `/app/model/info` with only a `model` parameter | API code `302` | That query shape was not sufficient; required parameters and code meaning were not established |
| Matter detail request against a U100 record | API code `7167` | No usable Matter detail from that request; do not assign a general error meaning or conclude every alternate control route is impossible |
| Legacy named-resource status query against U300 | Successful empty list | Try the model-appropriate numeric resources; an empty list alone is not a device-state value |
| U300 resource query without the `.1` suffix | Successful empty list | Use the app's full trait subscription path; the corrected read returned a value |
| CDN plugin download using a per-operation socket timeout | Slow transfer could continue much longer than expected | A socket timeout is not a whole-download deadline; bounded `curl --max-time` downloads completed and were checked by hash |

The community reference labels `106` as invalid signing, `810` as a wrong
password, and `818` as an account lock. Those interpretations were not
independently established through deliberate live failures here; `810`
is also used as an invented error response in the unit tests. Do not use
those fixtures as evidence of an actual authentication failure.

API failures are separate from HTTP failures: inspect the JSON `code` even
on a successful HTTP response. Preserve numeric diagnostics while keeping
server messages and response bodies out of user-visible errors and logs,
since they may include private data. Retry behavior, clock-skew tolerance,
and rate-limit responses have not been measured.

## Probe, validation, and remaining work

`tools/mobile_api.py` is an experimental standard-library client, excluded
from the Alfred release. Its fixed route allowlist permits only sign-in,
discovery, model-plugin lookup, and resource reads. Device writes and lock
credential enumeration are rejected before any network request. Redirects
are rejected and errors expose numeric codes without server messages.
Credentials and session tokens remain in memory.

`AppIdentity.from_reference()` reads literal constants from the separately
downloaded reference's `const.py` using Python's AST. It never executes the
reference or embeds its shared app keys in source control. Each sign-in
requires an explicit API area and country district; authentication is not
automatically retried.

The ten probe tests cover encryption, signing, reference parsing, session
handling, regional routing, Matter reads, the route allowlist, redirects,
and safe errors. The full suite passes 94 tests on Python 3.9 and 3.14.

### Reproducing the read-only probe

The module has no CLI and does not search password stores, decrypt caches,
or choose a home automatically. Obtain the pinned app-identity reference
separately and pass credentials to Python in memory. The following shows
the call sequence; arguments named `selected_*` must be supplied by the
caller from discovery, not hardcoded from another account:

```python
from tools.mobile_api import AppIdentity, MobileClient

identity = AppIdentity.from_reference(reference_const_path)
client = MobileClient(identity, selected_area, selected_district)
client.login(account_login, account_password)
homes = client.homes()
devices = client.devices(selected_home_id)
detail = client.details(selected_device_id)

# Select the branch from confirmed model/capability information.
if selected_model == "aqara.lock.acn10":
    status = client.status(selected_device_id)
elif selected_model == "aqara.matter.4447_10241":
    capabilities = client.matter_details(selected_device_id)
    # Derive selected_endpoint from this device's returned eps/funcs.
    path = str(selected_endpoint) + ".148.32970.1"
    status = client.matter_status(selected_device_id, [path])
```

This sequence illustrates the API and intentionally does not print raw
objects. The example is not a complete account/home picker. Call
`client.plugins(selected_model)` to retrieve plugin metadata, then filter
for the exact model. Keep download credentials separate from API headers.

To run the existing offline tests from the research checkout:

```sh
python3 -m unittest discover -s tests
/usr/bin/python3 -m unittest discover -s tests
git diff --check
```

The tests mock network responses; passing them does not reconfirm live
service availability, physical commands, session lifetime, or compatibility
with different lock firmware. The live reads reported in this document
were separate experiments. The client enforces a fixed host/route map,
rejects redirects, uses a 20-second socket timeout, and rejects responses
larger than 4 MiB. Its socket timeout is not a full wall-clock deadline.

### Remaining questions and next experiments

1. Trace `AHDoorLock.controlLockState` in a running app and identify its
   final destination, request body, device-password handling, and any
   additional cryptographic authorization. Merely finding `/matter/write`
   in the APK does not answer those questions.
2. Determine a supported U100 key-provisioning or transfer process. Establish
   how device permissions, Bluetooth login, key IDs, and existing phone keys
   interact before attempting any setup that changes the lock.
3. Validate the U100 timestamp, CRC, AES IV/padding, and representation
   against actual app output. No compatible encrypted command generator
   has been implemented yet.
4. Check token lifetime, refresh behavior, pagination, multi-home selection,
   account isolation, and model/firmware differences before considering a
   production mobile-account integration.
5. Once the wire protocol and authorization are understood, test a chosen
   control operation under an explicitly agreed physical test procedure.
   Verify both reported state and observed lock movement, then handle
   device errors and offline states before exposing it in Alfred.

The Android SDK/emulator, instrumented runtime, and capture harness were
absent during this investigation. No emulator, Frida session, authenticated
app network capture, or runtime native-code trace was used. Static analysis
exposed class/method and route strings in protected libraries but did not
recover the native control implementation. Lack of a decoded implementation
is a tooling limit, not proof that the API cannot support control.

The released workflow does not expose lock/unlock on the strength of static
evidence alone. Any future result must distinguish request acceptance,
reported state change, and physical movement.

APK, decoded resources, proprietary plugins, and downloaded reference code
remain in ignored `.cache/` storage and outside the release file allowlist.
Private credential caches remain outside both repository checkouts.

For continued research, retain evidence provenance (app/plugin version,
source URL, hash, and inspected module) and record only sanitized shapes,
technical model identifiers, and numeric diagnostics. Do not publish
account-to-region mappings, account or home names, device counts or IDs,
actual device states, credential-store item IDs, signed request headers,
pairing material, decrypted caches, or unredacted traffic captures.
Workflow packaging uses the explicit allowlist in `workflow-build`, which
excludes this document, the probe, all downloaded sources, and research
artifacts; documenting a route does not enable it in the shipped workflow.
