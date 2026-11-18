# Doc-derived NVUE API goldens (blind mode)

Response shapes for the fake `nvued` endpoint (`../httpapi.py`), derived
from documentation and the `nvidia.nvue` collection source — not from a
live switch. When read-only access to a real Cumulus 5.6+ switch is
available, capture the same responses and diff them against these
files; update the data files only, no code changes.

## Sources

Primary (the code that will actually run):

- `nvidia.nvue` collection, GitLab
  `nvidia-networking/systems-engineering/nvue`, `plugins/httpapi/httpapi.py`:
  the transport contract —
  - `GET /nvue_v1/{path}?rev=applied[&filled=false]`
  - `POST /nvue_v1/revision` → response is a dict whose first key is
    the new revision id
  - `PATCH /nvue_v1/{path}?rev=<id>` with a JSON body
  - `PATCH /nvue_v1/revision/<id>` with `{"state": "apply",
    "auto-prompt": {"ays": "ays_yes", "ignore_fail":
    "ignore_fail_yes"}}` when forced
  - poll `GET /nvue_v1/revision/<id>` until `state == "applied"`
  - request keys normalized: underscores → hyphens; lists of
    `{id: ..., ...}` become dicts keyed by id; `None` values are
    dropped by the plugin before sending
- `nvidia.nvue` collection, `plugins/modules/api.py`: module options
  (`operation: get|set`, `force`, `wait`, `path`, `filled`, `data`,
  `revid`).

Secondary:

- NVIDIA Cumulus Linux 5.9 "NVUE API" documentation (nginx fronting,
  port 8765, HTTPS, basic auth, 403 until the `cumulus` user password
  is changed)
- NVIDIA Galaxy `nvidia.nvue` docs: the "Delete system pre-login
  message" example proves **unset = `operation: set` with a `null`
  value**.
- NVUE CLI reference for the model paths (`nv set bridge domain ...`,
  `nv set interface ... bridge domain ...`).

## Files

| File | Represents |
|---|---|
| `get-bridge.json` | `GET /nvue_v1/bridge?rev=applied` |
| `get-interface.json` | `GET /nvue_v1/interface?rev=applied` |
| `get-system-global-reserved-vlan.json` | `GET /nvue_v1/system/global/reserved/vlan?rev=applied` |
| `post-revision.json` | `POST /nvue_v1/revision` response shape (revision id placeholder `<id>`) |
| `get-revision.json` | `GET /nvue_v1/revision/<id>` after a successful apply |

## Provider operation → API mapping

`nv unset` maps to `set` with `null` (see sources). Each current
network-runner Cumulus provider task becomes one `nvidia.nvue.api`
`operation: set`:

| Provider task | `data` payload |
|---|---|
| `create_vlan` | `{"bridge": {"domain": {"br_default": {"vlan": {"<id>": {}}}}}}` |
| `delete_vlan` | `{"bridge": {"domain": {"br_default": {"vlan": {"<id>": null}}}}}` |
| `conf_trunk_port` | `{"interface": {"<port>": {"bridge-domain": {"br_default": {}}}}}` |
| `conf_access_port` | `{"interface": {"<port>": {"bridge-domain": {"br_default": {"access": "<id>"}}}}}` |
| `add_trunk_vlan` | `{"interface": {"<port>": {"bridge-domain": {"br_default": {"vlan": {"<id>": {}}}}}}}` |
| `delete_trunk_vlan` | same with `"vlan": {"<id>": null}` |
| `delete_port` | `{"interface": {"<port>": {"bridge-domain": null}}}` |

## Assumption ledger

Items to verify against a real switch (or the collection issue
tracker) before relying on them; each has the fallback chosen in the
fake:

1. **Revision id shape.** `create_revision()` returns the first key of
   the response dict. Assumed to be a short string id (docs show
   `?rev=2`). Fallback: opaque string; the fake treats it as opaque.
2. **`nv unset` → `null` value.** Supported by the Galaxy module docs
   example; not visible in `httpapi.py` itself (the plugin drops
   `None` values — see next item). Fallback if wrong: DELETE method on
   the object path.
3. **Null dropping in `normalize_spec`.** The plugin removes `None`
   values while building the PATCH body, which would defeat the
   unset-via-null mapping above; the delete example works, so the
   module path must preserve nulls somewhere. Verify with the
   collection's own tests or a real switch. If the module truly drops
   nulls, unsets need `nvidia.nvue.api` bypassed (raw `uri`) or a
   patched module — **blocking question for phase 2**.
4. **Interface bridge-domain key spelling.** Assumed
   `bridge-domain` (hyphenated) per the plugin's key normalization.
   Verify with `nv set interface swp1 bridge domain br_default &&
   nv config apply` then `GET /nvue_v1/interface?rev=applied`.
5. **Reserved VLAN REST path and shape.** Assumed
   `/nvue_v1/system/global/reserved/vlan` returning
   `{"reserved": {...}}` (CLI: `nv show system global reserved vlan`).
   Defaults assumed from Cumulus docs (`1-36, 4045-4094`). Fallback:
   keep this one read task as a documented gap.
6. **Revision GET state values.** Assumed `applied` / `pending` /
   `apply` transitions as coded in the plugin's poll loop. Verify the
   exact terminal state string.
7. **`filled` parameter behavior.** Not modeled by the fake (always
   returns full objects). Harmless if the provider does not set
   `filled: false`.
8. **Auth.** Basic auth, 403 with the factory `cumulus` password;
   token/scheme details of nginx fronting not verified.
9. **Empty-object pruning.** The fake removes containers that a
   null-delete emptied (e.g. `interface.swp2` disappears when its
   `bridge-domain` is deleted) but keeps attribute-less leaf objects
   (`vlan 10: {}` is valid state). Verify against a real switch
   whether GETs return empty objects or omit them.
