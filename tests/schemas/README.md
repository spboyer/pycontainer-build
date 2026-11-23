# Validation schemas

These verbatim schemas are test fixtures used to validate generated documents:

- `oci-runtime-spec-v1.3.0/` contains the OCI runtime-spec v1.3.0 JSON schemas
  from <https://github.com/opencontainers/runtime-spec/tree/v1.3.0/schema>,
  used to validate merged security fragments. They are licensed under
  [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0); the license text
  is included in `LICENSE-APACHE-2.0.txt`. SHA-256:
  - `config-freebsd.json`: `1014c4bb0b4b31e90d182c43b0cf1352f77e280706786b726bcb622e24e44e39`
  - `config-linux.json`: `123bb6c446d0054d37f6cc8c569e72cbec5ca5a5a670cf7a77aa30b2834344bc`
  - `config-schema.json`: `220835b554f2bfb990c95700f9077f2f0a0643a7dbfd3b2676ac55e1aad1d1a3`
  - `config-solaris.json`: `7c7babccb26a27b266bc195b25d5832ed9fe412c716f9ce6c9702edcb2207c99`
  - `config-vm.json`: `d927fee38f523c343a471adb9cc56c0719aa6c300aa0c4191c5b9c0551d905c7`
  - `config-windows.json`: `b9ad0c1d38bb6f3bae4a6efeef33c1c1434ae3801d9d4cb435dc7d21ecf02e48`
  - `config-zos.json`: `409268930b22de85223f4b64ac357e6f70f72e4465e2e8c1051b32589736f4ef`
  - `defs-freebsd.json`: `8317c8ca406bc2c32fe01b720be81c3ff722bdeffab0bc3604399601730537af`
  - `defs-linux.json`: `75f7814a39067ce49f250919e546c99fd30f20b140e354a3248ae0c218874783`
  - `defs-vm.json`: `b204c703303a98783d13e576f583ab128e88219e77c62b2282361ed2a4f2c352`
  - `defs-windows.json`: `c0282c945629cbd124cf0389fd7c6e8d334b05a7a717d0efb860ff2ecd488c22`
  - `defs-zos.json`: `75ca042c97bbb3933ab2a216318468aa70b1e3aa82fc6b6a58f4d4d2df6be0fc`
  - `defs.json`: `db17c223dd9b3ffb4fb80ca46f11f1a809e6f983f13dd8296c9459ba686f8b8c`

- `spdx-schema-2.3.json` is the SPDX 2.3 schema from
  <https://github.com/spdx/spdx-spec/tree/v2.3/schemas>. The SPDX specification
  is Copyright the Linux Foundation and contributors and licensed under
  [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/).
  SHA-256: `239208b7ac287b3cf5d9a9af23f9d69863971102a5e1587a27a398b43490b89b`.
- `cyclonedx-bom-1.4.schema.json` is the CycloneDX 1.4 schema from
  <https://github.com/CycloneDX/specification/tree/master/schema>. It is
  licensed under [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0);
  the license text is included in `LICENSE-APACHE-2.0.txt`.
  SHA-256: `c22ea18d8ede3dbacc22bff3d3216fffe4c7c2b645a20af6aa223dceaaabb596`.
