# OSAC-7869: Cumulus provider migration to nvidia.nvue

Status: proposal (not yet accepted)
Owner: Connectivity&Fabric
Issue: [OSAC-7869](https://redhat.atlassian.net/browse/OSAC-7869)

## Summary

Replace the direct `nv` commands in network-runner's Cumulus switch
provider with modules from the [`nvidia.nvue`][galaxy] Ansible collection,
with the NVUE REST API as the stated target transport, while preserving
the switch-management behavior that AgentlessNet (Agentless VLAN Fabric
Manager) depends on: bridge-domain VLAN create/delete, trunk/access port
configuration, VLAN discovery, and the deletion-guard serialization.

[galaxy]: https://galaxy.ansible.com/ui/repo/published/nvidia/nvue/

## Current state

The provider is vendored from the [`ybettan/network-runner`][nr] Git
collection (version 0.4.0, see `collections/requirements.yml`) at
`vendor/ansible_collections/ansible_network/network_runner/roles/switch/providers/cumulus/`.

[nr]: https://github.com/ybettan/network-runner

Every mutating task (`create_vlan`, `delete_vlan`, `conf_access_port`,
`conf_trunk_port`, `add_trunk_vlan`, `delete_trunk_vlan`, `delete_port`)
runs one `raw` shell snippet over SSH that:

1. takes `flock /tmp/nvue.lock` to serialize with the NVUE config lock;
2. for guarded tasks, fails with exit code 73 when the guard file under
   `/var/lib/network-runner/guards` exists (delete-wins semantics);
3. runs `nv set`/`nv unset`;
4. validates `nv config diff --output commands` emits only
   `nv set`/`nv unset` lines;
5. runs `nv config apply -y` when there is a pending diff.

Read tasks (`list_vlans`, `get_port_conf`) use `bridge -j vlan show`,
`nv show system global reserved vlan --output json`, and `awk` over
`/etc/cumulus/switchd.conf`. `set_deletion_guard` uses only `flock` and
`install` (no `nv`).

## Why REST is the target

### The Netris precedent inside OSAC

OSAC's networking stack is already API-first wherever a controller
exists. The Netris Fabric Manager integration never touches a switch:
`collections/ansible_collections/netris/controller/` modules speak the
Netris controller REST API, the `osac.templates` `roles/netris` role
orchestrates them through the 19 contract tasks, and consistency is
controller-owned (`roles/netris/tasks/apply_network_attachment.yaml`
notes that serialization belongs to the controller). The transport is
held to a tested standard: session-cookie redirect isolation
(`tests/unit/test_netris_external_ip_transport.py`), enforced TLS
certificate validation (`tests/unit/test_netris_tls.py`,
`test_netris_validate_certs.py`), and `no_log` credential redaction.

The SSH + `raw` shell pattern survives only in the controller-less
case: AgentlessNet managing bare Cumulus switches directly. NVUE REST
(`nvued`, fronted by nginx) is architecturally the same shape as the
Netris controller API — a per-switch controller API. Migrating the
Cumulus provider to REST makes the controller-less path consistent
with the rest of the stack instead of baking SSH in further.

### The industry direction

SSH CLI scraping is the legacy tier for data-center NOSes. Arista EOS
exposes eAPI (JSON-RPC over HTTPS, ~2014), Cisco NX-OS offers
NX-API/NETCONF/RESTCONF/gNMI, IOS XE/XR speak NETCONF/RESTCONF with
gNMI on XR, Junos is NETCONF/YANG with growing gNMI, SONiC is
gNMI/REST against CONFIG_DB with OpenConfig models, and Nokia SR
Linux is gNMI-first. NVIDIA's own material positions NVUE as
API-first: the `nv` CLI is a client of the same `nvued` service the
REST API exposes. A provider abstraction standardized on
`ansible.netcommon.httpapi` is also the only shape that generalizes if
network-runner later grows non-Cumulus providers (Arista, SONiC), all
of which are API-connected.

### Switch prerequisites are smaller than first assessed

Since Cumulus Linux 5.6, the NVUE REST API is **enabled by default**
on port 8765 (HTTPS, basic auth). The remaining caveats are the
pre-5.6 brownfield (manual nginx enablement on 5.4/5.5) and day-0
state: the API returns 403 until the `cumulus` user's default
password is changed, and the default certificate is self-signed.
Both are standard customer hardening the REST path makes mandatory
and verifiable (see the risks section) rather than extra burdens.
The provider's supported floor should therefore be stated as
Cumulus 5.6+ for the REST path.

## Target design

### Phase 1 (interim, optional): `nvidia.nvue.command`

`nvidia.nvue.command` is a wrapper around the `nv` CLI with Jinja
templating, `apply`, and `assume_yes`, over the existing SSH
connection. It removes the direct `nv` invocations without any
inventory, fixture or switch-prerequisite change, and can land as a
single upstream release for fleets that include pre-5.6 switches. It
is a stepping stone only; it does not move the provider toward the
API-first architecture above.

- Each mutating task becomes a `raw` guard pre-check (flock + guard
  file, exit 73 refusal, no `nv`) followed by
  `nvidia.nvue.command` with the same `nv set`/`nv unset` statements.
- The `flock` serialization cannot wrap a module call; the provider
  relies on NVUE's own apply-time locking plus the pre-check.
- The diff-validation step is dropped (behavioral delta below).

### Phase 2 (target): NVUE REST modules over httpapi

Mutating and read tasks move to the `nvidia.nvue` REST modules
(`nvidia.nvue.bridge`, `nvidia.nvue.interface`, `nvidia.nvue.api`
for uncovered paths) with `ansible.netcommon.httpapi` and
`ansible_network_os: nvidia.nvue.httpapi`:

- **Inventory contract**: switch entries gain `ansible_host`,
  `ansible_httpapi_port: 8765`, `ansible_httpapi_use_ssl: true`, and
  REST credentials; SSH variables are dropped. The AgentlessNet VN
  inventory validation (`files/validate_vn_inventory.yml`, covered by
  the `agentless_net_stub` target) is extended to validate the REST
  endpoint shape and to keep rejecting password-based SSH access.
- **TLS policy**: `ansible_httpapi_validate_certs` defaults to true
  with a documented CA-injection path, following the Netris precedent
  where certificate validation cannot be bypassed silently. Customers
  install a proper internal-CA certificate via
  `nv set system api certificate`; the self-signed default is treated
  as day-0 convenience, not an operating state.
- **Deletion guards**: the exit-73 delete-wins protocol is
  reimplemented as an API-equivalent check — the guard directory
  moves to a provider-visible location read over the same transport,
  or the guard state moves into the caller (AgentlessNet already owns
  delete-wins state in SQLite; a provider-local guard may become
  unnecessary). The contract targets keep asserting the observable
  semantics: a guarded apply refuses, and delete wins the race.
- **Serialization**: local `flock` disappears. NVUE apply is atomic
  per revision; cross-task ordering stays the caller's
  responsibility, exactly as Netris delegates serialization to its
  controller.
- **Read tasks**: `list_vlans` moves to
  `nv show ... --output json` equivalents via the API
  (`nvidia.nvue.api` `operation: get`); `get_port_conf` can read
  bridge-domain state from the NVUE model instead of kernel
  `bridge -j vlan show`. Discovery semantics asserted by
  `tests/integration/targets/agentless_net_switch` (normalized VLAN
  discovery, reserved ranges) must be preserved or the delta
  documented.

## Delivery path (vendored content constraint)

`vendor/` content must not be hand-edited
([osac-aap/AGENTS.md](../AGENTS.md)). The provider change therefore
lands in two places, in order, per phase:

1. **Upstream PRs to `ybettan/network-runner`**: the provider task
   changes, their molecule/unit tests, a dependency declaration on
   `nvidia.nvue` (and `ansible.netcommon` for phase 2), and version
   bumps (0.5.0 for phase 1, 0.6.0 for phase 2).
2. **OSAC mono-repo PRs** (this branch's scope once upstream
   releases):
   - bump `ansible_network.network_runner` in
     `collections/requirements.yml`;
   - add `nvidia.nvue` (Galaxy-published, GPL-2.0-or-later, requires
     ansible-core >= 2.15, which the EE already satisfies) to
     `collections/requirements.yml` — `ansible.netcommon` is already
     vendored;
   - run `make vendor-collections` (needs Automation Hub credentials)
     and review the complete `vendor/` diff, per the component rules;
   - rebuild the execution environment
     (`make execution-environment-build`) since the EE image must
     carry `nvidia.nvue` for AAP job runs;
   - update the fixtures and tests below;
   - update the AgentlessNet VN inventory documentation for the REST
     endpoint and TLS requirements;
   - update `docs/INTEGRATION-TESTING.md` and the touched-area map
     only if commands or coverage boundaries change.

## Test impact

| Area | Change |
|---|---|
| `tests/integration/fixtures/agentless_switch/ssh` | Phase 1: must emulate the shipped-module protocol or expose a minimal `nv`/`python3` surface. Phase 2: replaced by an HTTP fake of `nvued` modeled on `tests/integration/fixtures/netris/fake_netris.py` (stateful, per-revision config, apply/rollback semantics); the SSH fake is retired with the SSH transport. |
| `tests/integration/targets/agentless_net_contract` | Partial switch-apply retry, delete-wins guard refusal, port restoration: keep asserting the observable semantics; adjust to REST error surfaces (HTTP status classes replacing exit codes, e.g. 73). |
| `tests/integration/targets/agentless_net_switch` | Phase 1: unchanged (read tasks not migrated). Phase 2: VLAN discovery and trunk reconciliation move onto the HTTP fake; normalized discovery and reserved ranges keep being asserted. |
| `tests/unit` (AgentlessNet switch-output mocks) | Update mocked transport output where provider task names or shapes change. |
| TLS/session handling | Follow the Netris precedent: unit tests proving the REST transport cannot silently skip certificate validation and that credentials stay redacted, mirroring `test_netris_tls.py`. |
| Wire goldens (`tests/contract/network-task`) | No change: the manager task contract, inputs, and results are untouched. |
| Coverage gate | Any new `when`/`rescue`/fail path in the provider needs a test or an entry in `tests/coverage/exclusions.yaml` — note the coverage gate measures `roles/netris`, `roles/cudn_evpn` only; provider files under `vendor/` are reported, not gated. |

## Behavioral deltas and risks

- **Diff validation removed** (both phases): a pending unrelated NVUE
  change could be applied together with the provider's change. NVUE
  apply is atomic per revision, so the blast radius is one apply
  carrying both changes. Flag to reviewers; keep if unacceptable.
- **Guard/apply no longer atomic under one lock**: in phase 1 the
  guard check and the module's apply are separate steps; in phase 2
  the guard moves into caller-owned state. Either way the delete-wins
  race is closed at a different layer than today's single `flock`
  region; the contract targets are the proof and must keep passing.
- **Switch floor raises to Cumulus 5.6+** for the REST path (NVUE
  REST default-enabled; 5.4/5.5 stay on phase 1 if needed). State the
  floor in the network-runner README.
- **Credential and certificate prerequisites are standard hardening
  the REST path enforces rather than imposes**: Cumulus 5.6+ refuses
  API requests (403) until the `cumulus` user's default password is
  changed, so any fleet actually using REST has already done it; the
  SSH path trusts the same change invisibly. The design requires
  proper certificates too — `ansible_httpapi_validate_certs: true`
  with a documented CA-injection path, as customers already run an
  internal CA for the Netris controller, Keycloak, AAP and the
  Kubernetes API in an OSAC deployment; the self-signed default is
  day-0 convenience only. Exposure is not widened: port 8765 needs
  the same management-network reachability SSH already requires, over
  a narrower surface (HTTPS with authentication and request
  auditability instead of an interactive shell), and can be scoped
  with `nv set system api listening-address`. Deployment
  documentation covers these as prerequisites, not risks.
- **Collection support level**: `nvidia.nvue` is community-supported
  ("best effort"). The httpapi plugin surface we depend on is small;
  pin the version in `collections/requirements.yml`.
- **Live-switch equivalence**: acceptance requires lab validation on a
  real Cumulus switch comparing `nv config show` before/after for the
  same task sequence. That is QE work under the
  [OSAC-4843](https://redhat.atlassian.net/browse/OSAC-4843) suite, not
  covered by the component targets.

## Acceptance criteria mapping

- Cumulus provider uses `nvidia.nvue` instead of direct `nv` commands
  for the migrated operations: phase 1 covers mutating tasks; phase 2
  covers mutating and read tasks over REST.
- Collection dependencies declared: upstream `nvidia.nvue`
  requirement in network-runner plus `collections/requirements.yml`
  in OSAC (with `ansible.netcommon` for phase 2).
- Equivalent behavior: contract targets assert guard refusal, retry
  and restoration semantics against the new transport; live-switch
  equivalence is QE-run under OSAC-4843; remaining gaps documented
  above.
