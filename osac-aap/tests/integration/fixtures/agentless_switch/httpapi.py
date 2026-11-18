#!/usr/bin/env python3
"""Stateful fake of the NVUE REST API (nvued) for switch task tests.

Answers the /nvue_v1 surface the network-runner Cumulus provider uses
once it moves to nvidia.nvue over ansible.netcommon.httpapi, so
AgentlessNet switch tasks can be exercised without a live switch.

The transport contract mirrors nvidia.nvue's own httpapi plugin:

  POST   /nvue_v1/revision                     create a revision; the
                                             response is a dict whose
                                             first key is the revision id
  PATCH  /nvue_v1/<path>?rev=<id>              stage a JSON body into the
                                             pending revision (RFC 7396
                                             merge-patch: null deletes)
  PATCH  /nvue_v1/revision/<id>                {"state": "apply", ...}
                                             commits the revision
  GET    /nvue_v1/<path>?rev=applied           read the applied tree
  GET    /nvue_v1/revision/<id>                {"state": "applied"} once
                                             committed

The provider operations map onto merge-patch bodies, for example
create_vlan is {"bridge": {"domain": {"br_default": {"vlan":
{"<id>": {}}}}}} and delete_vlan is the same with a null value. The
applied tree is kept under the same model paths, so the fake renders
GETs by subtree; static shapes that do not depend on state (the
reserved VLAN defaults) come from doc-derived golden files, see
doc-derived/README.md.

Auth models the Cumulus 5.6+ behavior: basic auth is required and the
factory password is refused with 403 until changed. The expected
credentials come from the environment (defaults below), so tests can
also exercise the 403 path.

Fault injection replaces the CLI fail marker of the old SSH fake: when
the marker file named by OSAC_AGENTLESS_SWITCH_FAIL_MARKER contains a
fault name, the matching step fails in the scripted way (see FAULTS).

Admin endpoints (not part of the NVUE API) let tests inspect and steer
the fake:

  GET  /__admin/state     the applied tree and pending revisions
  POST /__admin/reset     back to the empty state
  POST /__admin/shutdown  stop the server

Command line:

  python httpapi.py --port-file <file> [--state-file <file>]
                    [--goldens-dir <dir>] [--port N] [--tls-cert c --tls-key k]

writes the chosen port to the port file once it listens, and a JSON
snapshot of the applied tree to the state file after every commit.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PREFIX = "/nvue_v1"
DEFAULT_FACTORY_PASSWORD = "CumulusLinux!"
USERNAME = os.environ.get("OSAC_AGENTLESS_SWITCH_USER", "cumulus")
PASSWORD = os.environ.get("OSAC_AGENTLESS_SWITCH_PASSWORD", "changed-password")
FAULT_MARKER = Path(
    os.environ.get("OSAC_AGENTLESS_SWITCH_FAIL_MARKER", "/dev/null")
)

# Fault name -> behavior, read from the marker file between requests.
#   apply_partial: commit only the first patch of the revision, then
#                  answer the apply with 500 once (models the partial
#                  switch apply the contract target retries)
FAULTS = ("apply_partial",)

# Applied-tree defaults rendered for paths no patch has touched yet.
STATIC_PATHS = {
    "system/global/reserved/vlan": "get-system-global-reserved-vlan.json",
}


class State:
    """The applied model tree plus pending revisions."""

    def __init__(self, goldens: dict):
        self.goldens = goldens
        self.tree: dict = {}
        self.revisions: dict = {}
        self.next_id = 1

    def snapshot(self):
        return {"tree": self.tree, "revisions": self.revisions}

    def create_revision(self):
        while str(self.next_id) in self.revisions:
            self.next_id += 1
        revid = str(self.next_id)
        self.revisions[revid] = {"state": "pending", "patches": []}
        return revid

    def stage(self, revid, path, body):
        """Stage a patch: the URL path is part of the model tree."""
        patch = body
        for part in reversed([p for p in path.strip("/").split("/") if p]):
            patch = {part: patch}
        self.revisions[revid]["patches"].append(copy.deepcopy(patch))

    def apply(self, revid):
        revision = self.revisions[revid]
        fault = read_fault()
        patches = revision["patches"]
        if fault == "apply_partial" and len(patches) > 1:
            merge_patch(self.tree, patches[0])
            revision["patches"] = patches[1:]
            revision["state"] = "pending"
            raise FaultError(500, "partial apply (injected fault)")
        for patch in patches:
            merge_patch(self.tree, patch)
            prune_deleted(self.tree, patch)
        revision["patches"] = []
        revision["state"] = "applied"


class FaultError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def read_fault():
    try:
        value = FAULT_MARKER.read_text().strip()
    except OSError:
        return ""
    return value if value in FAULTS else ""


def merge_patch(target, patch):
    """RFC 7396 merge-patch: null deletes, objects merge, else replace."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    if not isinstance(target, dict):
        target = {}
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        else:
            target[key] = merge_patch(target.get(key), value)
    return target


def prune_deleted(target, patch, path=()):
    """Remove containers a null-delete emptied, along that path only.

    A leaf object with no attributes (``vlan 10: {}``) is valid NVUE
    state, so empty objects are never removed globally — only ancestors
    of an explicitly deleted key that became empty.
    """
    if not isinstance(patch, dict):
        return
    for key, value in patch.items():
        if value is None:
            # Walk from the deleted key upward, dropping empty parents.
            prefix = list(path) + [key]
            while prefix:
                node = subtree_at(target, prefix[:-1])
                if isinstance(node, dict) and node.get(prefix[-1]) == {}:
                    del node[prefix[-1]]
                prefix.pop()
        elif isinstance(value, dict):
            prune_deleted(target, value, path + (key,))


def subtree_at(node, path):
    for part in path:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def subtree(tree, path):
    node = tree
    for part in path.strip("/").split("/") if path.strip("/") else []:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def load_goldens(directory):
    goldens = {}
    if directory:
        for name in os.listdir(directory):
            if name.endswith(".json"):
                with open(Path(directory) / name, encoding="utf-8") as fh:
                    goldens[name] = json.load(fh)
    return goldens


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-nvued/0.1"

    def log_message(self, fmt, *args):  # keep test output quiet
        pass

    # -- plumbing -----------------------------------------------------

    def send_json(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    def authorized(self):
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(
                header.split(None, 1)[1]
            ).decode().partition(":")
        except ValueError:
            return False
        if password == DEFAULT_FACTORY_PASSWORD:
            return False  # Cumulus refuses the factory credential
        return user == USERNAME and password == PASSWORD

    # -- request routing ----------------------------------------------

    def do_GET(self):
        if not self.check_auth():
            return
        url = urlsplit(self.path)
        if url.path == "/__admin/state":
            return self.send_json(200, self.server.state.snapshot())
        if url.path == "/__admin/shutdown":
            self.send_json(200, {"bye": True})
            threading.Thread(target=self.server.shutdown).start()
            return
        path = url.path[len(PREFIX):].rstrip("/") or "/"
        query = parse_qs(url.query)
        rev = (query.get("rev") or ["applied"])[0]
        if path.startswith("/revision/"):
            revid = path[len("/revision/"):]
            revision = self.server.state.revisions.get(revid)
            if revision is None:
                return self.send_json(404, {"error": "no such revision"})
            return self.send_json(200, {"state": revision["state"]})
        if path == "/revision":
            return self.send_json(
                200,
                {key: {"state": value["state"]}
                 for key, value in self.server.state.revisions.items()},
            )
        if rev != "applied":
            return self.send_json(400, {"error": "rev must be applied"})
        name = path.strip("/")
        if name in STATIC_PATHS:
            golden = self.server.state.goldens.get(STATIC_PATHS[name])
            if golden is not None:
                served = {k: v for k, v in golden.items()
                          if not k.startswith("_")}
                return self.send_json(200, served)
        value = subtree(self.server.state.tree, path)
        if value is None:
            return self.send_json(404, {"error": f"no object at {name}"})
        return self.send_json(200, value)

    def do_POST(self):
        if not self.check_auth():
            return
        url = urlsplit(self.path)
        if url.path == PREFIX + "/revision":
            revid = self.server.state.create_revision()
            return self.send_json(200, {revid: {"state": "pending"}})
        return self.send_json(404, {"error": "unknown path"})

    def do_PATCH(self):
        if not self.check_auth():
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        body = self.read_body()
        path = url.path[len(PREFIX):].rstrip("/")
        if path.startswith("/revision/"):
            revid = path[len("/revision/"):]
            revision = self.server.state.revisions.get(revid)
            if revision is None:
                return self.send_json(404, {"error": "no such revision"})
            if body.get("state") != "apply":
                return self.send_json(400, {"error": "only apply is supported"})
            try:
                self.server.state.apply(revid)
            except FaultError as fault:
                return self.send_json(fault.status, {"error": fault.message})
            self.server.write_state()
            return self.send_json(200, {"state": "applied"})
        revid = (query.get("rev") or [""])[0]
        revision = self.server.state.revisions.get(revid)
        if revision is None:
            return self.send_json(404, {"error": "no such revision"})
        self.server.state.stage(revid, path, body)
        return self.send_json(200, {})

    def check_auth(self):
        if self.path.startswith("/__admin/"):
            return True
        if self.authorized():
            return True
        self.send_json(403, {"error": "authentication required"})
        return False


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, state: State, state_file):
        super().__init__(address, Handler)
        self.state = state
        self.state_file = state_file

    def write_state(self):
        if self.state_file:
            self.state_file.write_text(
                json.dumps(self.state.snapshot(), sort_keys=True)
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port-file", required=True)
    parser.add_argument("--state-file")
    parser.add_argument("--goldens-dir")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    goldens = load_goldens(args.goldens_dir)
    state = State(goldens)
    state_file = Path(args.state_file) if args.state_file else None
    server = Server(("127.0.0.1", args.port), state, state_file)
    port_file = Path(args.port_file)
    port_file.parent.mkdir(parents=True, exist_ok=True)
    port_file.write_text(str(server.server_address[1]))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
