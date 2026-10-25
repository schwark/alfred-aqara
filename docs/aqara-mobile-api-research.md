# Aqara mobile API research

Investigated 2026-10-24 for the Alfred Aqara workflow. Static analysis used
JADX 1.5.6 and APKTool 3.0.3, following the local
`scraper-tools:discover-mobile-api` skill.

## Finding

The official Android APK contains private mobile API paths. An independent
community implementation provides a concrete D100 cloud-control lead.
Compatibility with other lock models is still unknown. No mobile login,
lock/unlock command, or authenticated traffic capture was performed.

## Evidence collected directly from the APK

Downloaded from Aqara's own
[download page](https://cdn.aqara.com/cdn/luoshu/mainland/static/app-download/index.html),
which links the international APK at
https://cdn.aqara.com/cdn/luoshu/mainland/static/app/aqara-home.apk.

- Package: `com.lumiunited.aqarahome.play`.
- Version: 6.4.0 (7011), the version served by that URL during inspection.
- Size: 264,622,915 bytes.
- SHA-256: `518e75081778234f193926450f03b602297a5fa1523a92513b4bb5cb36267bd4`.
- Most Java implementations are behind SecNeo components, including
  `com.secneo.apkwrapper`, `libDexHelper`, `libdexjni`, and `libdatajar`.
- `libdatajar.so` nevertheless contains readable API strings and lock class
  names. The React Native base bundle contains Hermes bytecode.

Route suffixes found below `/app/v1.0/lumi` include:

| Route | Evidence |
| --- | --- |
| `/user/guard-code/login` | APK string |
| `/app/position/query/home/list` | APK string |
| `/app/position/device/query` | APK string |
| `/matter/write` | APK string |
| `/dev/lock/query` | APK string |
| `/dev/bluetooth/login/assure/publickey` | APK string |
| `/dev/bluetooth/login/assure/verify` | APK string |
| `/devex/lock/p2p/info` | APK string |
| `/devex/lock/p2p/sign` | APK string |

A string establishes a candidate route, not its method, payload, permissions,
or behavior. The exact strings and offsets are recorded in the ignored
`.cache/analysis-evidence.json`; some strings omit the leading slash.

## Independent implementation reviewed

The community project
[aquara-local](https://github.com/home-assistant-plugins/aquara-local),
commit `4fc274688cd40774307549172c9ae1db073c92ca`, is MIT licensed and
unaffiliated with Aqara or Home Assistant. Its author reports cloud lock/unlock
for the D100 through `/matter/write`; the README says that path does not
unlock the A100 Pro. Its Python client uses a separate mobile-account session.
Those claims were not tested on the user's hardware. LAN-control descriptions
conflict across its documents, so LAN unlock remains unresolved.

The [API reference](https://github.com/home-assistant-plugins/aquara-local/blob/4fc274688cd40774307549172c9ae1db073c92ca/docs/API.md)
describes mobile login and signed requests. Its discovery and authentication
route names match strings extracted from Aqara's APK. Matching routes do not
validate its signing constants or command behavior for this app version.

## Next validation

An Agent device-list response can expose endpoint ID/name, device name/type,
and room name/ID without a hardware model. The generic DoorLock type does not
establish which mobile control protocol to use. Obtain the exact hardware model
from device information or authenticated mobile discovery.

A read-only mobile login and home/device-list probe can then establish account
region, authentication compatibility, and actual model identifiers. The Agent
bearer key is a separate credential. Any mobile secrets must be kept outside
source control. An accepted request must not be described as confirmed physical
lock movement without checking the resulting device state.

Dynamic tracing and authenticated mobile replay have not been performed.
Static APK inspection alone does not establish a working mobile session or
validate a control payload. Private session tokens and account credentials
must remain outside the repository, reports, and workflow release assets.

## Model-specific checks

Aqara's [U100 FAQ](https://www.aqara.com/en/product/smart-lock-u100/faq/)
documents Bluetooth 5.0 and Zigbee 3.0, and remote access through Aqara Home.
Its mobile cloud-control payload still requires direct validation; the generic
Agent control action table does not provide lock/unlock.

Aqara's [U300 announcement](https://www.aqara.com/en/?p=93236) identifies it
as a lever lock for non-deadbolt doors with native Matter over Thread support.
Native Matter support does not establish that Aqara's private mobile endpoint
uses the same trait IDs, endpoint numbers, or authorization as the D100.

The D100 community implementation remains a model-specific lead. Its
`2.148.35010.0` and `2.148.35011.0` traits must not be offered as generic
lock/unlock commands for U100 or U300 without model-specific evidence.
Next steps are a read-only mobile login and device discovery, followed by
inspection of model capabilities and the matching app plugin or captured
request. Command acceptance and observed lock movement are separate checks.

## Read-only probe implementation

`tools/mobile_api.py` is an experimental standard-library client for validating
the documented mobile sign-in and discovery wire formats. It permits only the
login, home list, device list, device detail, and resource-query routes. Control
routes and lock credential enumeration are rejected before any network request.
HTTPS redirects are rejected; request failures expose numeric codes without
server response messages. Passwords and session tokens are kept in memory.

The `AppIdentity.from_reference()` loader reads literal app constants from the
separately downloaded reference's `const.py` using Python's AST; it does not
execute the reference code or embed shared app keys in this repository. The
reference identifies its wire identity as app version 6.1.6. Acceptance by the
current service remains unverified; this is separate from inspection of the
official 6.4.0 APK. Every sign-in requires an explicit API area and country
district. Authentication is not retried automatically.

The module is excluded from the workflow release's explicit file allowlist.
Its nine tests cover encryption, signing, reference parsing, session handling,
the route allowlist, redirects, and safe failure messages. The complete suite
passes 93 tests on Python 3.9 and 3.14. No authenticated result is claimed yet.

APK, libraries, decoded resources, and third-party reference source are kept in
`.cache/`, ignored by Git and excluded from Alfred releases. The installed
workflow remains unchanged by this research.
