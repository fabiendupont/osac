#!/usr/bin/env bash
# Smoke test for the fake nvued endpoint. Run from anywhere.
set -e
cd "$(dirname "$0")"
pkill -f "fixtures/agentless_switch/httpapi.py" 2>/dev/null || true
sleep 0.5
OSAC_AGENTLESS_SWITCH_PASSWORD=changed-password \
OSAC_AGENTLESS_SWITCH_FAIL_MARKER=/tmp/opencode/nvue.fault \
python3 httpapi.py --port-file /tmp/opencode/nvue.port \
  --state-file /tmp/opencode/nvue.state --goldens-dir doc-derived --port 18765 &
trap 'curl -s -u cumulus:changed-password http://127.0.0.1:18765/__admin/shutdown >/dev/null 2>&1 || true' EXIT
sleep 1
PORT=18765
AUTH="cumulus:changed-password"
H='Content-Type: application/json'
BASE="http://127.0.0.1:$PORT"
new_rev() { curl -s -u "$AUTH" -X POST $BASE/nvue_v1/revision | python3 -c 'import json,sys; print(next(iter(json.load(sys.stdin))))'; }

echo "== 403 with factory password =="
curl -s -o /dev/null -w "%{http_code}\n" -u "cumulus:CumulusLinux!" "$BASE/nvue_v1/bridge?rev=applied"

echo "== create vlan 10/20, access swp1, trunk-vlan swp2 =="
REV=$(new_rev)
curl -s -u "$AUTH" -X PATCH "$BASE/nvue_v1/?rev=$REV" -H "$H" -d '{"bridge":{"domain":{"br_default":{"vlan":{"10":{},"20":{}}}}}}' >/dev/null
curl -s -u "$AUTH" -X PATCH "$BASE/nvue_v1/?rev=$REV" -H "$H" -d '{"interface":{"swp1":{"bridge-domain":{"br_default":{"access":"10"}}}}}' >/dev/null
curl -s -u "$AUTH" -X PATCH "$BASE/nvue_v1/?rev=$REV" -H "$H" -d '{"interface":{"swp2":{"bridge-domain":{"br_default":{"vlan":{"10":{}}}}}}}' >/dev/null
curl -s -u "$AUTH" -X PATCH "$BASE/nvue_v1/revision/$REV" -H "$H" -d '{"state":"apply"}'; echo
curl -s -u "$AUTH" "$BASE/nvue_v1/?rev=applied" | python3 -m json.tool
echo "-- subtree reads --"
curl -s -u "$AUTH" "$BASE/nvue_v1/bridge?rev=applied"; echo
curl -s -u "$AUTH" "$BASE/nvue_v1/interface/swp1?rev=applied"; echo
echo "-- reserved vlans (golden, no _source leak) --"
curl -s -u "$AUTH" "$BASE/nvue_v1/system/global/reserved/vlan?rev=applied"; echo

echo "== deletes: vlan 20, port swp2 (null semantics) =="
REV=$(new_rev)
curl -s -u "$AUTH" -X PATCH "$BASE/nvue_v1/?rev=$REV" -H "$H" -d '{"bridge":{"domain":{"br_default":{"vlan":{"20":null}}}}}' >/dev/null
curl -s -u "$AUTH" -X PATCH "$BASE/nvue_v1/?rev=$REV" -H "$H" -d '{"interface":{"swp2":{"bridge-domain":null}}}' >/dev/null
curl -s -u "$AUTH" -X PATCH "$BASE/nvue_v1/revision/$REV" -H "$H" -d '{"state":"apply"}'; echo
curl -s -u "$AUTH" "$BASE/nvue_v1/?rev=applied" | python3 -m json.tool

echo "== partial-apply fault, then retry =="
echo "apply_partial" > /tmp/opencode/nvue.fault
REV=$(new_rev)
curl -s -u "$AUTH" -X PATCH "$BASE/nvue_v1/?rev=$REV" -H "$H" -d '{"bridge":{"domain":{"br_default":{"vlan":{"30":{}}}}}}' >/dev/null
curl -s -u "$AUTH" -X PATCH "$BASE/nvue_v1/?rev=$REV" -H "$H" -d '{"interface":{"swp1":{"bridge-domain":{"br_default":{"access":"30"}}}}}' >/dev/null
curl -s -o /dev/null -w "apply status: %{http_code}\n" -u "$AUTH" -X PATCH "$BASE/nvue_v1/revision/$REV" -H "$H" -d '{"state":"apply"}'
rm -f /tmp/opencode/nvue.fault
echo "-- partial state: vlan 30 present, swp1 still access 10 --"
curl -s -u "$AUTH" "$BASE/nvue_v1/bridge?rev=applied"; echo
curl -s -u "$AUTH" "$BASE/nvue_v1/interface/swp1?rev=applied"; echo
echo "-- retry the same revision --"
curl -s -u "$AUTH" -X PATCH "$BASE/nvue_v1/revision/$REV" -H "$H" -d '{"state":"apply"}'; echo
curl -s -u "$AUTH" "$BASE/nvue_v1/?rev=applied" | python3 -m json.tool
