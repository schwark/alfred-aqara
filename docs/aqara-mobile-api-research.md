# Aqara mobile API research

Investigated 2026-10-24 for the Alfred Aqara workflow, following the local
`scraper-tools:discover-mobile-api` skill. This report contains technical
protocol findings only; account details, device identifiers, credentials,
session tokens, and returned device states are omitted.

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

No pairing key has been extracted, no Bluetooth pairing has been attempted,
and no remote-function setting has been changed. A separate mobile control
route could exist; the inspected branch does not establish one.

The community D100 implementation reports `/matter/write` control with
`pwd` and `type` fields. That report is model-specific and does not validate
the same operation for U100.

## U300: Matter capabilities and native authorization

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

The same module contains a read-only password-state query at
`/devex/device/pwd/state/query` and password-management writes at
`/devex/lock/pwd`. None were called. In particular, its function named
`validateDevicePassword` uses a password-change request; it must not be
treated as a harmless read-only validation endpoint.

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

Next work is to trace the native U300 authorization path and determine a
supported way to provision or access U100's remote key. An instrumented
Android runtime is not installed; no emulator traffic capture has occurred.
Any eventual control test must distinguish request acceptance, reported
state change, and physical movement. No lock/unlock command is exposed in
the released workflow on the strength of static evidence alone.

APK, decoded resources, proprietary plugins, and downloaded reference code
remain in ignored `.cache/` storage and outside the release file allowlist.
Private credential caches remain outside both repository checkouts.
