#!/usr/bin/env python3
"""Create (or update) every AAP controller + EDA object the NetLab demo uses.

Idempotent: objects are matched by name. Secrets (NetBox API token, NetBox
admin password, event stream token) are generated only when the credential
holding them doesn't exist yet, and the NetBox admin password is written to
--secrets-out so a person can log in.

  AAP_URL=https://gateway.aap.ramzidev.com AAP_TOKEN=<oauth token> \
    python3 bootstrap/aap_bootstrap.py --secrets-out ~/.netlab-secrets.json

Stdlib only (no requests) so it runs anywhere Python 3.8+ does.
"""
import argparse
import json
import os
import secrets
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ----------------------------------------------------------------- settings
REPO_URL = "https://github.com/ramzihusein/netlab-automation.git"
BRANCH = "main"
ORG = "Default"
EE = "Default execution environment"
NETLAB_EE = "NetLab EE"            # built from execution-environments/netlab-ee/
LINUX_EE = "Linux EE"              # built from execution-environments/linux-ee/
BACKUP_REPO = "ramzihusein/netlab-backups"   # private: running configs hold secrets
G = "/api/gateway/v1"
HUB_REGISTRY = "gateway.aap.ramzidev.com"   # push (from the lab host, TLS verify off)
# AAP pulls by a node name: the gateway certificates are issued (by the AAP
# installer CA, which execution nodes trust) for node hostnames, not for the
# public DNS name. Same registry as the built-in "Hub ... execution environment"s.
HUB_PULL_REGISTRY = "ip-10-0-1-147.us-east-2.compute.internal"
DE = "Default Decision Environment"
AWS_CRED = "AWS Creds"
SSH_CRED = "EC2"
LOCAL_INVENTORY = "Demo Inventory"
NETBOX_URL = "http://netbox-int.aap.ramzidev.com:8000"

PROVISION_VARS = {
    "aws_region": "us-east-2",
    "vpc_id": "vpc-0da5834914cfa99c1",
    "subnet_id": "subnet-0ca01e4e00b0dd13a",
    "route_table_id": "rtb-0ce766d3ed898722c",
    "key_pair_name": "aap-demo-key",
    "aap_security_group_ids": ["sg-0d84199bb9a2f42f1", "sg-02bfb3ef3396a2631"],
    "route53_zone_id": "Z01488022EYNCATJ5U5X8",
    "admin_cidrs": ["76.204.63.213/32"],
}

C = "/api/controller/v2"
E = "/api/eda/v1"

# ---------------------------------------------------------------- http glue
BASE = os.environ.get("AAP_URL", "").rstrip("/")
TOKEN = os.environ.get("AAP_TOKEN", "")
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE   # AAP gateway uses a self-signed certificate


class ApiError(Exception):
    pass


def api(method, path, body=None, ok=(200, 201, 202, 204)):
    url = path if path.startswith("http") else BASE + path
    data = json.dumps(body).encode() if body is not None else None
    auth = TOKEN if TOKEN.lower().startswith("bearer ") else "Bearer " + TOKEN
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": auth, "Content-Type": "application/json", "Accept": "application/json"})
    # Reads are retried on connection errors (gateway DNS round-robin can hit a
    # node that resets the connection); writes are not, to avoid double-POSTs.
    for attempt in range(3 if method == "GET" else 1):
        try:
            with urllib.request.urlopen(req, context=_ctx, timeout=120) as r:
                text = r.read().decode()
                status = r.status
            break
        except urllib.error.HTTPError as e:
            text, status = e.read().decode(), e.code
            break
        except (urllib.error.URLError, ConnectionError):
            if attempt == 2 or method != "GET":
                raise
            time.sleep(3)
    if status not in ok:
        raise ApiError(f"{method} {path} -> {status}: {text[:1500]}")
    return json.loads(text) if text else {}


def find(path, name, **filters):
    # EDA's ?name= filter matches substrings ("NetLab NetBox Changes" also
    # matches "... (AWS SQS)"), so insist on an exact match.
    q = urllib.parse.urlencode(dict(name=name, page_size=200, **filters))
    res = [r for r in api("GET", f"{path}?{q}").get("results", []) if r.get("name") == name]
    return res[0] if res else None


def ensure(path, name, body, update=True, **filters):
    """Create path/<name> or patch it. Returns (obj, created)."""
    obj = find(path, name, **filters)
    if obj is None:
        obj = api("POST", path, dict(body, name=name))
        print(f"  + {path.split('/')[-2]}: {name}")
        return obj, True
    if update:
        obj = api("PATCH", f"{path}{obj['id']}/", body)
    print(f"  = {path.split('/')[-2]}: {name}")
    return obj, False


def wait(path, field, done, failed=(), timeout=600):
    end = time.time() + timeout
    while time.time() < end:
        obj = api("GET", path)
        if obj.get(field) in done:
            return obj
        if obj.get(field) in failed:
            raise ApiError(f"{path} {field}={obj.get(field)}: {json.dumps(obj)[:1500]}")
        time.sleep(5)
    raise ApiError(f"timeout waiting for {path} {field} in {done}")


def yaml_list(text):
    """Parse EDA's source_mappings (a YAML list of flat dicts) without PyYAML."""
    items = []
    for line in (text or "").splitlines():
        if line.startswith("- "):
            items.append({})
            line = line[2:]
        key, _, value = line.strip().partition(":")
        if items and key:
            items[-1][key.strip()] = value.strip().strip("'\"")
    return items


def yaml_vars(d):
    # JSON is valid YAML; AAP stores extra_vars/source_vars as text.
    return json.dumps(d, indent=2)


# ------------------------------------------------------------------- steps
def credential_types():
    print("Credential types")
    netbox, _ = ensure(f"{C}/credential_types/", "NetBox API", {
        "kind": "cloud",
        "description": "NetBox URL + API token (inventory plugin env and playbook vars)",
        "inputs": {"fields": [
            {"id": "url", "label": "NetBox URL", "type": "string"},
            {"id": "token", "label": "API token", "type": "string", "secret": True},
            {"id": "admin_password", "label": "Admin password", "type": "string", "secret": True},
        ], "required": ["url", "token"]},
        "injectors": {
            "env": {"NETBOX_API": "{{ url }}", "NETBOX_TOKEN": "{{ token }}"},
            "extra_vars": {"netbox_url": "{{ url }}", "netbox_token": "{{ token }}",
                           "netbox_admin_password": "{{ admin_password }}"},
        },
    })
    stream, _ = ensure(f"{C}/credential_types/", "EDA Event Stream Target", {
        "kind": "cloud",
        "description": "Where an external system should POST events for an EDA event stream",
        "inputs": {"fields": [
            {"id": "url", "label": "Event stream URL", "type": "string"},
            {"id": "header", "label": "Auth header name", "type": "string", "default": "Authorization"},
            {"id": "token", "label": "Auth header value", "type": "string", "secret": True},
        ], "required": ["url", "header", "token"]},
        "injectors": {"extra_vars": {
            "eda_event_stream_url": "{{ url }}",
            "eda_event_stream_header": "{{ header }}",
            "eda_event_stream_token": "{{ token }}",
        }},
    })
    login, _ = ensure(f"{C}/credential_types/", "Container Registry Login", {
        "kind": "cloud",
        "description": "Registry host + login as playbook vars (for building and pushing EEs)",
        "inputs": {"fields": [
            {"id": "host", "label": "Registry host", "type": "string"},
            {"id": "username", "label": "Username", "type": "string"},
            {"id": "password", "label": "Password", "type": "string", "secret": True},
        ], "required": ["host", "username"]},
        "injectors": {"extra_vars": {
            "ee_registry": "{{ host }}",
            "ee_registry_username": "{{ username }}",
            "ee_registry_password": "{{ password }}",
        }},
    })
    repo, _ = ensure(f"{C}/credential_types/", "GitHub Repo Token", {
        "kind": "cloud",
        "description": "A GitHub repo (owner/name) and a token that can push to it",
        "inputs": {"fields": [
            {"id": "repo", "label": "Repository (owner/name)", "type": "string"},
            {"id": "token", "label": "Token (fine-grained, Contents read/write)", "type": "string", "secret": True},
        ], "required": ["repo"]},
        "injectors": {"extra_vars": {"backup_repo": "{{ repo }}", "backup_repo_token": "{{ token }}"}},
    })
    return netbox, stream, login, repo


def backup_credential(org_id, repo_ct):
    print("Config backup repo")
    cred = find(f"{C}/credentials/", "NetLab Config Backups")
    if cred is None:
        token = os.environ.get("BACKUP_REPO_TOKEN", "")
        if not token:
            print("  ! no BACKUP_REPO_TOKEN: set the token on 'NetLab Config Backups' in the AAP UI")
        cred, _ = ensure(f"{C}/credentials/", "NetLab Config Backups", {
            "credential_type": repo_ct["id"], "organization": org_id,
            "description": f"Push device config backups to github.com/{BACKUP_REPO} (private)",
            "inputs": {"repo": BACKUP_REPO, "token": token},
        })
    else:
        print("  = credentials: NetLab Config Backups (token kept)")
    return cred


def hub_registry(org_id, login_ct):
    """Hub login twice over: a Container Registry credential so AAP can pull the
    NetLab EE, and a Container Registry Login so the build job can push it."""
    print("Automation hub registry")
    pull, push = find(f"{C}/credentials/", "NetLab Hub Registry"), find(f"{C}/credentials/", "NetLab Hub Push")
    if pull is None or push is None:
        user, password = os.environ.get("HUB_USERNAME", "admin"), os.environ.get("HUB_PASSWORD", "")
        if not password:
            print(f"  ! no HUB_PASSWORD: set the password for '{user}' on both hub credentials in the AAP UI")
        registry = api("GET", f"{C}/credential_types/?namespace=registry")["results"][0]
        pull, _ = ensure(f"{C}/credentials/", "NetLab Hub Registry", {
            "credential_type": registry["id"], "organization": org_id,
            "description": "Pull the NetLab EE from the automation hub",
            "inputs": {"host": HUB_PULL_REGISTRY, "username": user, "password": password, "verify_ssl": True},
        })
        push, _ = ensure(f"{C}/credentials/", "NetLab Hub Push", {
            "credential_type": login_ct["id"], "organization": org_id,
            "description": "Push the NetLab EE to the automation hub (NetLab - Build Execution Environment)",
            "inputs": {"host": HUB_REGISTRY, "username": user, "password": password},
        })
    else:
        api("PATCH", f"{C}/credentials/{pull['id']}/", {"inputs": dict(
            pull["inputs"], host=HUB_PULL_REGISTRY, verify_ssl=True)})   # password stays "$encrypted$" = kept
        print("  = credentials: NetLab Hub Registry / NetLab Hub Push (secrets kept)")
    ee, _ = ensure(f"{C}/execution_environments/", NETLAB_EE, {
        "organization": org_id, "image": f"{HUB_PULL_REGISTRY}/netlab-ee:latest", "pull": "always",
        "credential": pull["id"],
        "description": "Supported EE + netbox.netbox and pynetbox (execution-environments/netlab-ee)",
    })
    # Same hub, same pull credential: one EE per use case.
    ensure(f"{C}/execution_environments/", LINUX_EE, {
        "organization": org_id, "image": f"{HUB_PULL_REGISTRY}/linux-ee:latest", "pull": "always",
        "credential": pull["id"],
        "description": "Supported EE + community.general, community.crypto, containers.podman "
                       "(execution-environments/linux-ee)",
    })
    return push, ee


def event_stream(org_id, eda_cts, stream_ct, controller_org_id, stream_name, eda_cred_name,
                 target_name, description):
    """An EDA event stream with its own token, plus a controller credential
    holding the URL + token for whatever posts to it. Returns the stream."""
    target = find(f"{C}/credentials/", target_name)
    stream_cred = find(f"{E}/eda-credentials/", eda_cred_name)
    new_token = None
    if target is None or stream_cred is None:
        new_token = secrets.token_urlsafe(32)
        stream_cred, _ = ensure(f"{E}/eda-credentials/", eda_cred_name, {
            "credential_type_id": eda_cts["Token Event Stream"], "organization_id": org_id,
            "description": f"Token for event stream {stream_name}",
            "inputs": {"auth_type": "token", "token": new_token, "http_header_key": "Authorization"},
        })
    else:
        print(f"  = eda-credentials: {eda_cred_name} (token kept)")
    stream, _ = ensure(f"{E}/event-streams/", stream_name, {
        "eda_credential_id": stream_cred["id"], "organization_id": org_id, "test_mode": False,
    }, update=False)
    stream["url"] = BASE + urllib.parse.urlsplit(stream["url"]).path
    if new_token is not None:
        ensure(f"{C}/credentials/", target_name, {
            "credential_type": stream_ct["id"], "organization": controller_org_id,
            "description": description,
            "inputs": {"url": stream["url"], "header": "Authorization", "token": new_token},
        })
    else:
        print(f"  = credentials: {target_name} (token kept)")
    return stream


def eda_side(org_id, stream_ct):
    print("EDA credentials + event stream")
    eda_cts = {c["name"]: c["id"] for c in api("GET", f"{E}/credential-types/?page_size=200")["results"]}
    aap_cred, _ = ensure(f"{E}/eda-credentials/", "NetLab AAP", {
        "credential_type_id": eda_cts["Red Hat Ansible Automation Platform"],
        "organization_id": org_id,
        "description": "Lets NetLab rulebook activations launch controller jobs",
        "inputs": {"host": f"{BASE}/api/controller/", "oauth_token": TOKEN.split()[-1],
                   "verify_ssl": False},
    })

    target = find(f"{C}/credentials/", "NetLab EDA Event Stream")
    stream_cred = find(f"{E}/eda-credentials/", "NetLab NetBox Webhook")
    new_token = None
    if target is None or stream_cred is None:
        new_token = secrets.token_urlsafe(32)
        stream_cred, _ = ensure(f"{E}/eda-credentials/", "NetLab NetBox Webhook", {
            "credential_type_id": eda_cts["Token Event Stream"],
            "organization_id": org_id,
            "description": "Shared token NetBox sends in its webhook Authorization header",
            "inputs": {"auth_type": "token", "token": new_token, "http_header_key": "Authorization"},
        })
    else:
        print("  = eda-credentials: NetLab NetBox Webhook (token kept)")

    stream, _ = ensure(f"{E}/event-streams/", "NetLab NetBox", {
        "eda_credential_id": stream_cred["id"], "organization_id": org_id, "test_mode": False,
    }, update=False)

    # EDA may advertise a single node's internal hostname; post via the gateway instead.
    path = urllib.parse.urlsplit(stream["url"]).path
    stream["url"] = BASE + path
    if new_token is not None:
        ensure(f"{C}/credentials/", "NetLab EDA Event Stream", {
            "credential_type": stream_ct["id"], "organization": org_id,
            "description": "NetBox webhook target (EDA event stream NetLab NetBox)",
            "inputs": {"url": stream["url"], "header": "Authorization", "token": new_token},
        })
    else:
        print("  = credentials: NetLab EDA Event Stream (token kept)")
    return aap_cred, stream


def controller_credentials(org_id, netbox_ct, secrets_out):
    print("Controller credentials")
    nb = find(f"{C}/credentials/", "NetLab NetBox")
    if nb is None:
        token, password = secrets.token_hex(20), secrets.token_urlsafe(18)
        nb, _ = ensure(f"{C}/credentials/", "NetLab NetBox", {
            "credential_type": netbox_ct["id"], "organization": org_id,
            "description": "NetBox API for the NetLab demo (token is created by Install NetBox)",
            "inputs": {"url": NETBOX_URL, "token": token, "admin_password": password},
        })
        if secrets_out:
            path = os.path.expanduser(secrets_out)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"netbox_url": NETBOX_URL.replace("netbox-int", "netbox"),
                           "netbox_admin_user": "admin", "netbox_admin_password": password,
                           "netbox_api_token": token}, f, indent=2)
            print(f"  NetBox admin password written to {path}")
    else:
        print("  = credentials: NetLab NetBox (secrets kept)")

    machine = api("GET", f"{C}/credential_types/?namespace=ssh")["results"][0]
    dev, _ = ensure(f"{C}/credentials/", "NetLab Devices", {
        "credential_type": machine["id"], "organization": org_id,
        "description": "containerlab default login for cEOS / IOL / c8000v",
        "inputs": {"username": "admin", "password": "admin"},
    })
    return nb, dev


def project(org_id):
    print("Controller project")
    proj, _ = ensure(f"{C}/projects/", "NetLab Automation", {
        "organization": org_id, "scm_type": "git", "scm_url": REPO_URL, "scm_branch": BRANCH,
        "scm_update_on_launch": True, "scm_update_cache_timeout": 60,
        "description": "containerlab + NetBox + EDA network demo",
    })
    upd = api("POST", f"{C}/projects/{proj['id']}/update/")
    wait(f"{C}/project_updates/{upd['id']}/", "status", {"successful"}, {"failed", "error", "canceled"})
    print("  project synced")
    return proj


def inventories(org_id, proj, aws_id, nb_cred):
    print("Inventories")
    infra, _ = ensure(f"{C}/inventories/", "NetLab Infra", {
        "organization": org_id, "description": "NetLab EC2 hosts (lab host, NetBox) by tag Role"})
    ensure(f"{C}/inventory_sources/", "NetLab EC2", {
        "inventory": infra["id"], "source": "ec2", "credential": aws_id,
        "overwrite": True, "update_on_launch": True, "update_cache_timeout": 0,
        "source_vars": yaml_vars({
            "regions": ["us-east-2"],
            "filters": {"tag:Project": "netlab", "instance-state-name": "running"},
            "keyed_groups": [{"key": "tags.Role", "prefix": "role"}],
            "hostnames": ["private-ip-address"],
            "compose": {"ansible_user": "'ubuntu'"},
        }),
    }, inventory=infra["id"])

    devices, _ = ensure(f"{C}/inventories/", "NetLab Devices", {
        "organization": org_id, "description": "Lab network devices from NetBox (tag netlab)"})
    ensure(f"{C}/inventory_sources/", "NetBox", {
        "inventory": devices["id"], "source": "scm", "source_project": proj["id"],
        "source_path": "inventories/netbox.yml", "credential": nb_cred["id"],
        "overwrite": True, "overwrite_vars": True, "update_on_launch": True, "update_cache_timeout": 0,
    }, inventory=devices["id"])
    return infra, devices


def job_templates(org_id, proj, ee_id, netlab_ee_id, inv, creds):
    print("Job templates")
    pv = {k: PROVISION_VARS[k] for k in ("vpc_id", "route_table_id", "route53_zone_id")}
    specs = [
        ("NetLab - Provision Infra", "playbooks/01_provision_infra.yml", "local", ["aws"],
         {"extra_vars": yaml_vars(PROVISION_VARS), "ask_variables_on_launch": True}),
        ("NetLab - Configure Lab Host", "playbooks/02_configure_lab_host.yml", "infra", ["aws", "ssh"], {}),
        ("NetLab - Deploy Lab", "playbooks/03_deploy_lab.yml", "infra", ["ssh"],
         {"ask_variables_on_launch": True}),
        ("NetLab - Install NetBox", "playbooks/04_install_netbox.yml", "infra", ["ssh", "netbox"], {}),
        # netbox.netbox modules need pynetbox, which only the NetLab EE has.
        ("NetLab - Populate NetBox", "playbooks/05_populate_netbox.yml", "infra", ["ssh", "netbox"],
         {"execution_environment": netlab_ee_id}),
        ("NetLab - Configure Devices", "playbooks/configure_network.yml", "devices", ["devices", "netbox"],
         {"ask_limit_on_launch": True, "ask_variables_on_launch": True}),
        # Same playbook in check + diff mode: reports drift from NetBox, changes nothing.
        ("NetLab - Check Config Drift", "playbooks/configure_network.yml", "devices", ["devices", "netbox"],
         {"job_type": "check", "diff_mode": True, "ask_limit_on_launch": True}),
        # One feature each (thin playbooks around a single role), for focused demos.
        ("NetLab - Configure Base Services", "playbooks/configure_base_services.yml", "devices", ["devices", "netbox"],
         {"ask_limit_on_launch": True, "ask_variables_on_launch": True}),
        ("NetLab - Configure VLANs", "playbooks/configure_vlans.yml", "devices", ["devices", "netbox"],
         {"ask_limit_on_launch": True, "ask_variables_on_launch": True}),
        ("NetLab - Configure Interfaces", "playbooks/configure_interfaces.yml", "devices", ["devices", "netbox"],
         {"ask_limit_on_launch": True, "ask_variables_on_launch": True}),
        ("NetLab - Configure ACLs", "playbooks/configure_acls.yml", "devices", ["devices", "netbox"],
         {"ask_limit_on_launch": True, "ask_variables_on_launch": True}),
        ("NetLab - Configure BGP", "playbooks/configure_bgp.yml", "devices", ["devices", "netbox"],
         {"ask_limit_on_launch": True, "ask_variables_on_launch": True}),
        ("NetLab - Configure NetBox Webhook", "playbooks/07_configure_netbox_webhook.yml", "infra",
         ["ssh", "netbox", "stream", "ingest"], {"survey_enabled": True}),
        ("NetLab - Build Execution Environment", "playbooks/09_build_ee.yml", "infra", ["ssh", "hub_push"],
         {"survey_enabled": True}),
        ("NetLab - Configure Syslog Forwarder", "playbooks/10_configure_syslog_forwarder.yml", "infra",
         ["ssh", "syslog_stream"], {}),
        # Day-2 operations
        ("NetLab - Backup Configs", "playbooks/backup_configs.yml", "devices", ["devices", "backup"],
         {"ask_limit_on_launch": True}),
        ("NetLab - Restore Configs", "playbooks/restore_configs.yml", "devices", ["devices", "backup", "netbox"],
         {"ask_limit_on_launch": True, "ask_variables_on_launch": True, "survey_enabled": True,
          "execution_environment": netlab_ee_id}),
        ("NetLab - Validate Fabric", "playbooks/validate_fabric.yml", "devices", ["devices", "netbox"],
         {"ask_limit_on_launch": True, "ask_variables_on_launch": True}),
        # Self-heal: the same playbook as Configure Devices, run by EDA for one
        # device someone changed by hand; diff mode shows what was put back.
        ("NetLab - Self-Heal Device", "playbooks/configure_network.yml", "devices", ["devices", "netbox"],
         {"ask_limit_on_launch": True, "diff_mode": True}),
        # Steps of the "NetLab - Request Server VLAN" workflow
        ("NetLab - Add Server VLAN to NetBox", "playbooks/request_vlan_netbox.yml", "local", ["netbox"],
         {"ask_variables_on_launch": True, "execution_environment": netlab_ee_id}),
        ("NetLab - Demo Fault Injection", "playbooks/demo_fault.yml", "devices", ["devices"],
         {"ask_variables_on_launch": True}),
        # Getting started: config from plain variables files with resource
        # modules, no NetBox. Run from AAP, so self-heal leaves the changes
        # alone; "NetLab - Check Drift and Remediate" puts NetBox's config back.
        ("NetLab Intro 1 - Read Device Config as Data", "playbooks/intro_1_read_config.yml", "devices",
         ["devices"], {"ask_limit_on_launch": True}),
        ("NetLab Intro 2 - Configure VLANs", "playbooks/intro_2_configure_vlans.yml", "devices",
         ["devices"], {"diff_mode": True, "survey_enabled": True, "ask_variables_on_launch": True}),
        ("NetLab Intro 3 - Configure Interface Descriptions", "playbooks/intro_3_configure_interfaces.yml",
         "devices", ["devices"], {"diff_mode": True, "survey_enabled": True, "ask_limit_on_launch": True}),
        ("NetLab Intro 4 - Configure Banner", "playbooks/intro_4_configure_banner.yml",
         "devices", ["devices"], {"diff_mode": True, "survey_enabled": True, "ask_limit_on_launch": True}),
        ("NetLab - Stop", "playbooks/08_stop_netlab.yml", "local", ["aws"],
         {"ask_variables_on_launch": True}),
        ("NetLab - Teardown", "playbooks/99_teardown.yml", "local", ["aws"],
         {"extra_vars": yaml_vars(pv), "survey_enabled": True}),
    ]
    jts = {}
    for name, playbook, inv_key, cred_keys, extra in specs:
        jt, _ = ensure(f"{C}/job_templates/", name, dict({
            "job_type": "run", "organization": org_id, "project": proj["id"], "playbook": playbook,
            "inventory": inv[inv_key], "execution_environment": ee_id,
            "description": f"NetLab demo: {playbook}",
        }, **extra))
        have = {c["id"] for c in api("GET", f"{C}/job_templates/{jt['id']}/credentials/")["results"]}
        for key in cred_keys:
            if creds.get(key) and creds[key] not in have:
                api("POST", f"{C}/job_templates/{jt['id']}/credentials/", {"id": creds[key]})
        jts[name] = jt

    api("POST", f"{C}/job_templates/{jts['NetLab - Teardown']['id']}/survey_spec/", {
        "name": "Teardown", "description": "",
        "spec": [
            {"question_name": "Type netlab to confirm", "variable": "confirm_teardown",
             "type": "text", "required": True, "default": ""},
            {"question_name": "Also delete the S3 image bucket?", "variable": "delete_images",
             "type": "multiplechoice", "choices": ["false", "true"], "default": "false", "required": True},
        ],
    })
    api("POST", f"{C}/job_templates/{jts['NetLab - Restore Configs']['id']}/survey_spec/", {
        "name": "Restore point", "description": "",
        "spec": [{
            "question_name": "Backup to restore",
            "question_description": "main = the latest backup, or a commit ID from the backup repo history",
            "variable": "backup_ref", "type": "text", "default": "main", "required": False,
        }],
    })
    api("POST", f"{C}/job_templates/{jts['NetLab Intro 2 - Configure VLANs']['id']}/survey_spec/", {
        "name": "Resource module state", "description": "",
        "spec": [{
            "question_name": "How should the VLAN list be applied?",
            "question_description": "merged = add/update the listed VLANs only; replaced = listed VLANs keep "
                                    "only the settings in the file; overridden = the file is the full list, "
                                    "every other VLAN is deleted (host1 loses VLAN 10); deleted = remove "
                                    "the listed VLANs",
            "variable": "vlan_state", "type": "multiplechoice",
            "choices": ["merged", "replaced", "overridden", "deleted"], "default": "merged", "required": True,
        }],
    })
    api("POST", f"{C}/job_templates/{jts['NetLab Intro 3 - Configure Interface Descriptions']['id']}/survey_spec/", {
        "name": "Resource module state", "description": "",
        "spec": [{
            "question_name": "How should the interface data be applied?",
            "question_description": "merged = set the listed descriptions only; replaced = listed interfaces "
                                    "keep only the settings in the file",
            "variable": "interface_state", "type": "multiplechoice",
            "choices": ["merged", "replaced"], "default": "merged", "required": True,
        }],
    })
    api("POST", f"{C}/job_templates/{jts['NetLab Intro 4 - Configure Banner']['id']}/survey_spec/", {
        "name": "Banner", "description": "",
        "spec": [
            {"question_name": "Which banner?",
             "question_description": "login = shown before the password prompt; motd = message of the day",
             "variable": "banner_type", "type": "multiplechoice", "choices": ["login", "motd"],
             "default": "motd", "required": True},
            {"question_name": "Banner message", "variable": "banner_text", "type": "textarea",
             "default": "Change window tonight 22:00-23:00. Contact the network team before making changes.",
             "required": True, "min": 1, "max": 1000},
        ],
    })
    api("POST", f"{C}/job_templates/{jts['NetLab - Build Execution Environment']['id']}/survey_spec/", {
        "name": "Execution environment", "description": "",
        "spec": [{
            "question_name": "Which execution environment?",
            "question_description": "netlab-ee = NetBox content; linux-ee = Linux / infrastructure content. "
                                    "Definitions: execution-environments/<name>/ in the project",
            "variable": "ee_name", "type": "multiplechoice", "choices": ["netlab-ee", "linux-ee"],
            "default": "netlab-ee", "required": True,
        }],
    })
    ensure(f"{C}/schedules/", "NetLab - Backup Configs before shutdown", {
        "unified_job_template": jts["NetLab - Backup Configs"]["id"],
        "description": "Weekdays 18:45 ET, before the power schedule stops the lab",
        "rrule": "DTSTART;TZID=America/New_York:20261013T184500 RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR",
        "enabled": True,
    })
    api("POST", f"{C}/job_templates/{jts['NetLab - Configure NetBox Webhook']['id']}/survey_spec/", {
        "name": "Event path", "description": "",
        "spec": [{
            "question_name": "NetBox event path",
            "question_description": "keep = leave the active path as is (used by the Start workflow)",
            "variable": "netbox_event_path", "type": "multiplechoice",
            "choices": ["keep", "direct", "aws"], "default": "keep",
            # Optional: the Start workflow launches without answers; the
            # playbook default (keep) then applies.
            "required": False,
        }],
    })
    return jts


def workflow(org_id, jts):
    print("Workflow")
    wf, _ = ensure(f"{C}/workflow_job_templates/", "NetLab - Start (build if needed)", {
        "organization": org_id,
        "description": "Builds NetLab, or starts a stopped one: instances, DNS, lab, NetBox, config, EDA, health checks",
    })
    nodes_path = f"{C}/workflow_job_templates/{wf['id']}/workflow_nodes/"
    existing = {n["identifier"]: n for n in api("GET", nodes_path + "?page_size=100")["results"]}
    if existing:
        # Added later: the syslog forwarder, once the lab host is set up.
        if "syslog" not in existing and "labhost" in existing:
            n = api("POST", nodes_path, {"unified_job_template": jts["NetLab - Configure Syslog Forwarder"]["id"],
                                         "identifier": "syslog"})
            api("POST", f"{C}/workflow_job_template_nodes/{existing['labhost']['id']}/success_nodes/", {"id": n["id"]})
            print("  + workflow node: syslog forwarder")
        print("  = workflow nodes exist")
        return wf
    order = [
        ("provision", "NetLab - Provision Infra", False),
        ("labhost", "NetLab - Configure Lab Host", False),
        ("deploy", "NetLab - Deploy Lab", False),
        ("netbox", "NetLab - Install NetBox", False),
        ("populate", "NetLab - Populate NetBox", True),
        ("configure", "NetLab - Configure Devices", False),
        ("webhook", "NetLab - Configure NetBox Webhook", False),
        ("syslog", "NetLab - Configure Syslog Forwarder", False),
    ]
    node = {}
    for ident, jt, converge in order:
        node[ident] = api("POST", nodes_path, {
            "unified_job_template": jts[jt]["id"], "identifier": ident,
            "all_parents_must_converge": converge})["id"]
    for parent, child in [("provision", "labhost"), ("provision", "netbox"), ("labhost", "deploy"),
                          ("deploy", "populate"), ("netbox", "populate"), ("populate", "configure"),
                          ("configure", "webhook"), ("labhost", "syslog")]:
        api("POST", f"{C}/workflow_job_template_nodes/{node[parent]}/success_nodes/", {"id": node[child]})
    print("  + workflow nodes")
    return wf


def vlan_workflow(org_id, jts):
    """Self-service change with guardrails: backup + pre-check, approval,
    NetBox, push, (optional demo fault), post-check, restore on failure."""
    print("Workflow: Request Server VLAN")
    wf, _ = ensure(f"{C}/workflow_job_templates/", "NetLab - Request Server VLAN", {
        "organization": org_id, "survey_enabled": True,
        "description": "Self-service: add a server VLAN. Backup + pre-check, approval, NetBox, push, "
                       "post-check; automatic restore if the post-check fails",
    })
    api("POST", f"{C}/workflow_job_templates/{wf['id']}/survey_spec/", {
        "name": "Server VLAN request", "description": "",
        "spec": [
            {"question_name": "VLAN ID", "variable": "vlan_id", "type": "integer", "min": 2, "max": 4094,
             "default": 50, "required": True},
            {"question_name": "VLAN name", "variable": "vlan_name", "type": "text", "default": "ANALYTICS",
             "required": True},
            {"question_name": "Leaf switch", "variable": "changed_device", "type": "multiplechoice",
             "choices": ["eos-leaf1", "ios-leaf2"], "default": "eos-leaf1", "required": True},
            {"question_name": "Gateway (IP/prefix)", "variable": "gateway_cidr", "type": "text",
             "default": "172.16.50.1/24", "required": True},
            {"question_name": "Simulate a bad change (demo the automatic restore)", "variable": "demo_fault",
             "type": "multiplechoice", "choices": ["no", "yes"], "default": "no", "required": True},
        ],
    })
    nodes_path = f"{C}/workflow_job_templates/{wf['id']}/workflow_nodes/"
    if api("GET", nodes_path)["count"]:
        print("  = workflow nodes exist")
        return wf

    def node(ident, jt=None, extra=None):
        body = {"identifier": ident}
        if jt:
            body["unified_job_template"] = jts[jt]["id"]
        if extra:
            body["extra_data"] = extra
        return api("POST", nodes_path, body)["id"]

    n = {
        "backup": node("backup", "NetLab - Backup Configs"),
        "precheck": node("precheck", "NetLab - Validate Fabric"),
        "approval": node("approval"),
        "netbox": node("netbox", "NetLab - Add Server VLAN to NetBox"),
        "push": node("push", "NetLab - Configure Devices"),
        "fault": node("fault", "NetLab - Demo Fault Injection"),
        "postcheck": node("postcheck", "NetLab - Validate Fabric", {"check_new_vlan": True}),
        "restore": node("restore", "NetLab - Restore Configs"),
    }
    # The approval step is its own kind of node.
    api("POST", f"{C}/workflow_job_template_nodes/{n['approval']}/create_approval_template/", {
        "name": "Network lead approval", "timeout": 3600,
        "description": "Pre-checks passed and configs are backed up. Approve to add the VLAN.",
    })
    links = [("backup", "success", "precheck"), ("precheck", "success", "approval"),
             ("approval", "success", "netbox"), ("netbox", "success", "push"),
             ("push", "success", "fault"), ("fault", "always", "postcheck"),
             ("push", "failure", "restore"), ("postcheck", "failure", "restore")]
    for parent, kind, child in links:
        api("POST", f"{C}/workflow_job_template_nodes/{n[parent]}/{kind}_nodes/", {"id": n[child]})
    print("  + workflow nodes")
    return wf


def servicenow_side(org_id, proj, netlab_ee_id, devices_inv_id):
    """ServiceNow developer instance: NetBox stays the source of truth for the
    network; this job copies the devices into the CMDB."""
    print("ServiceNow")
    sn_ct, _ = ensure(f"{C}/credential_types/", "ServiceNow Instance", {
        "kind": "cloud",
        "description": "ServiceNow instance + login, as the env vars the servicenow.itsm collection reads",
        "inputs": {"fields": [
            {"id": "host", "label": "Instance URL (https://devNNNNN.service-now.com)", "type": "string"},
            {"id": "username", "label": "Username", "type": "string"},
            {"id": "password", "label": "Password", "type": "string", "secret": True},
        ], "required": ["host", "username", "password"]},
        # SN_TIMEOUT: a developer instance can take well over the default 10 s
        # to answer a CMDB update.
        "injectors": {"env": {"SN_HOST": "{{ host }}", "SN_USERNAME": "{{ username }}",
                              "SN_PASSWORD": "{{ password }}", "SN_TIMEOUT": "60"}},
    })
    cred = find(f"{C}/credentials/", "NetLab ServiceNow")
    if cred is None:
        if not os.environ.get("SN_HOST"):
            print("  ! no SN_HOST / SN_USERNAME / SN_PASSWORD: skipping ServiceNow")
            return None
        cred, _ = ensure(f"{C}/credentials/", "NetLab ServiceNow", {
            "credential_type": sn_ct["id"], "organization": org_id,
            "description": "ServiceNow developer instance (CMDB / ITSM demo)",
            "inputs": {"host": os.environ["SN_HOST"], "username": os.environ["SN_USERNAME"],
                       "password": os.environ["SN_PASSWORD"]},
        })
    else:
        print("  = credentials: NetLab ServiceNow (secrets kept)")
    jt, _ = ensure(f"{C}/job_templates/", "NetLab - Sync Network Devices to CMDB", {
        "job_type": "run", "organization": org_id, "project": proj["id"],
        "playbook": "playbooks/servicenow_sync_network.yml", "inventory": devices_inv_id,
        "execution_environment": netlab_ee_id,
        "description": "NetBox -> ServiceNow CMDB (one way; NetBox is the source of truth)",
    })
    have = {c["id"] for c in api("GET", f"{C}/job_templates/{jt['id']}/credentials/")["results"]}
    if cred["id"] not in have:
        api("POST", f"{C}/job_templates/{jt['id']}/credentials/", {"id": cred["id"]})
    return cred


def linux_side(org_id, proj, creds, sn_cred=None):
    """Linux configuration management demo: servers web1/web2/db1 (EC2), their
    inventory, and the job templates, all on the Linux EE.

    With a ServiceNow credential the CMDB owns the servers: the inventory is
    read from it and Provision builds what it lists. Without one, the
    inventory is read from EC2 tags."""
    print("Linux demo")
    linux_ee = find(f"{C}/execution_environments/", LINUX_EE)["id"]
    source_from = "the ServiceNow CMDB" if sn_cred else "EC2 tag Project=netlab-linux"
    inv, _ = ensure(f"{C}/inventories/", "Linux Servers", {
        "organization": org_id, "description": f"Linux demo servers ({source_from})"})
    sources = {
        "Linux EC2": {
            "source": "ec2", "credential": creds["aws"],
            "source_vars": yaml_vars({
                "regions": ["us-east-2"],
                "filters": {"tag:Project": "netlab-linux", "instance-state-name": "running"},
                "hostnames": ["tag:Name"],
                # Groups named after the tag values: web, db, rhel, ubuntu.
                "keyed_groups": [{"key": "tags.Role", "separator": ""}, {"key": "tags.OS", "separator": ""}],
                "compose": {"ansible_host": "private_ip_address",
                            "ansible_user": "'ec2-user' if tags.OS == 'rhel' else 'ubuntu'"},
            }),
        },
        # Same hosts and groups, from the CMDB records.
        "Linux CMDB": {
            "source": "scm", "source_project": proj["id"], "execution_environment": linux_ee,
            "source_path": "inventories/linux_servers.now.yml",
            "credential": sn_cred["id"] if sn_cred else None,
        },
    }
    use = "Linux CMDB" if sn_cred else "Linux EC2"
    ensure(f"{C}/inventory_sources/", use, dict(sources[use], **{
        "inventory": inv["id"], "overwrite": True, "update_on_launch": True, "update_cache_timeout": 0,
    }), inventory=inv["id"])
    # One source only: two would each own the same host names.
    for other in set(sources) - {use}:
        old = find(f"{C}/inventory_sources/", other, inventory=inv["id"])
        if old:
            api("DELETE", f"{C}/inventory_sources/{old['id']}/")
            print(f"  - inventory_sources: {other}")
    local = find(f"{C}/inventories/", LOCAL_INVENTORY)["id"]
    pv = {k: PROVISION_VARS[k] for k in ("vpc_id", "subnet_id", "key_pair_name", "aap_security_group_ids",
                                          "route53_zone_id", "admin_cidrs")}
    servers = inv["id"]
    specs = [
        ("Linux - Provision / Start Servers", "playbooks/linux_provision.yml", local, ["aws", "servicenow"],
         {"extra_vars": yaml_vars(pv), "survey_enabled": True}),
        ("Linux - Request Server in CMDB", "playbooks/servicenow_request_server.yml", local, ["servicenow"],
         {"survey_enabled": True}),
        ("Linux - Server Inventory Report", "playbooks/linux_report.yml", servers, ["ssh"],
         {"ask_limit_on_launch": True}),
        ("Linux - Manage Users", "playbooks/linux_users.yml", servers, ["ssh"],
         {"diff_mode": True, "ask_limit_on_launch": True}),
        ("Linux - Install Packages", "playbooks/linux_packages.yml", servers, ["ssh"],
         {"survey_enabled": True, "ask_limit_on_launch": True}),
        ("Linux - Apply Config Baseline", "playbooks/linux_baseline.yml", servers, ["ssh"],
         {"diff_mode": True, "ask_limit_on_launch": True}),
        # Same playbook in check + diff mode: shows drift from the baseline, changes nothing.
        ("Linux - Check Baseline Drift", "playbooks/linux_baseline.yml", servers, ["ssh"],
         {"job_type": "check", "diff_mode": True, "ask_limit_on_launch": True}),
        ("Linux - RHEL System Roles (time sync + SELinux)", "playbooks/linux_rhel_system_roles.yml", servers,
         ["ssh"], {"diff_mode": True}),
        ("Linux - Patch Servers", "playbooks/linux_patching.yml", servers, ["ssh"],
         {"survey_enabled": True, "ask_limit_on_launch": True}),
        ("Linux - Deploy Web App", "playbooks/linux_webapp.yml", servers, ["ssh"],
         {"survey_enabled": True}),
        ("Linux - Manage a Service", "playbooks/linux_service.yml", servers, ["ssh"],
         {"survey_enabled": True, "ask_limit_on_launch": True}),
    ]
    creds = dict(creds, servicenow=sn_cred["id"] if sn_cred else None)
    if not sn_cred:
        specs = [spec for spec in specs if spec[0] != "Linux - Request Server in CMDB"]
    jts = {}
    for name, playbook, inventory, cred_keys, extra in specs:
        jt, _ = ensure(f"{C}/job_templates/", name, dict({
            "job_type": "run", "organization": org_id, "project": proj["id"], "playbook": playbook,
            "inventory": inventory, "execution_environment": linux_ee,
            "description": f"Linux demo: {playbook}",
        }, **extra))
        have = {c["id"] for c in api("GET", f"{C}/job_templates/{jt['id']}/credentials/")["results"]}
        for key in cred_keys:
            if creds[key] and creds[key] not in have:
                api("POST", f"{C}/job_templates/{jt['id']}/credentials/", {"id": creds[key]})
        jts[name] = jt

    def survey(jt_name, title, spec):
        api("POST", f"{C}/job_templates/{jts[jt_name]['id']}/survey_spec/",
            {"name": title, "description": "", "spec": spec})

    def choice(question, variable, choices, default, description=""):
        return {"question_name": question, "question_description": description, "variable": variable,
                "type": "multiplechoice", "choices": choices, "default": default, "required": True}

    rebuild_q = choice("Start from brand-new servers?", "rebuild", ["no", "yes"], "no",
                       "yes = terminate web1/web2/db1 and build fresh ones from the RHEL/Ubuntu images")
    survey("Linux - Provision / Start Servers", "Servers", [rebuild_q])
    if sn_cred:
        survey("Linux - Request Server in CMDB", "Server", [
            {"question_name": "Server name (empty = the standard fleet web1, web2, db1)",
             "variable": "server_name", "type": "text", "default": "", "required": False,
             "question_description": "Lower case, e.g. web3"},
            choice("Operating system", "server_os", ["rhel", "ubuntu"], "rhel"),
            choice("Role", "server_role", ["web", "db"], "web"),
            choice("Request or retire", "request_action", ["request", "retire"], "request",
                   "request = new CMDB record (Pending Install); retire = mark it Retired. "
                   "Provision / Start Servers then builds or removes it."),
        ])
    survey("Linux - Install Packages", "Packages", [
        {"question_name": "Extra packages (comma separated, optional)", "variable": "extra_packages",
         "type": "text", "default": "", "required": False,
         "question_description": "On top of vars/linux/packages.yml, e.g. tree,wget"},
        choice("What to do with them", "package_state", ["present", "latest", "absent"], "present",
               "present = install if missing; latest = also update; absent = remove"),
    ])
    survey("Linux - Patch Servers", "Patching", [
        choice("Patch scope", "patch_scope", ["report", "security", "all"], "report",
               "report = list pending updates only; security = security updates; all = every update"),
        choice("Reboot", "reboot_policy", ["if_needed", "never"], "if_needed",
               "if_needed = reboot only when the update requires it"),
    ])
    survey("Linux - Deploy Web App", "Release", [
        {"question_name": "Version", "variable": "app_version", "type": "text", "default": "1.0",
         "required": True},
        {"question_name": "Message on the page", "variable": "app_message", "type": "text",
         "default": "Hello from Ansible Automation Platform", "required": True},
        choice("Page colour", "app_color", ["red", "blue", "green", "purple"], "red"),
    ])
    survey("Linux - Manage a Service", "Service", [
        choice("Service", "service_name", ["web server", "SSH", "time sync", "firewall"], "web server"),
        choice("Action", "service_action", ["status", "restarted", "started", "stopped"], "status"),
    ])

    # From nothing to a configured, deployed fleet in one run.
    wf, _ = ensure(f"{C}/workflow_job_templates/", "Linux - Build Server Fleet", {
        "organization": org_id,
        "description": "Provision/start servers, packages, users, baseline, RHEL System Roles, web app, report",
    })
    api("PATCH", f"{C}/workflow_job_templates/{wf['id']}/", {"survey_enabled": True})
    api("POST", f"{C}/workflow_job_templates/{wf['id']}/survey_spec/",
        {"name": "Fleet", "description": "", "spec": [rebuild_q]})
    nodes_path = f"{C}/workflow_job_templates/{wf['id']}/workflow_nodes/"
    if not api("GET", nodes_path)["count"]:
        chain = ["Linux - Provision / Start Servers", "Linux - Install Packages", "Linux - Manage Users",
                 "Linux - Apply Config Baseline", "Linux - RHEL System Roles (time sync + SELinux)",
                 "Linux - Deploy Web App", "Linux - Server Inventory Report"]
        ids = [api("POST", nodes_path, {"unified_job_template": jts[name]["id"],
                                         "identifier": name.split(" - ", 1)[1]})["id"] for name in chain]
        for parent, child in zip(ids, ids[1:]):
            api("POST", f"{C}/workflow_job_template_nodes/{parent}/success_nodes/", {"id": child})
        print("  + workflow nodes")
    return inv, jts


def remediate_workflow(org_id, jts):
    """Show the drift, approve, put NetBox's config back, prove it's in sync.
    Also the quick way to reset the lab after a demo."""
    print("Workflow: Check Drift and Remediate")
    wf, _ = ensure(f"{C}/workflow_job_templates/", "NetLab - Check Drift and Remediate", {
        "organization": org_id,
        "description": "Report drift from NetBox, approve, make the network match NetBox again, "
                       "then check again",
    })
    nodes_path = f"{C}/workflow_job_templates/{wf['id']}/workflow_nodes/"
    if api("GET", nodes_path)["count"]:
        print("  = workflow nodes exist")
        return wf

    def node(ident, jt=None):
        body = {"identifier": ident}
        if jt:
            body["unified_job_template"] = jts[jt]["id"]
        return api("POST", nodes_path, body)["id"]

    n = {
        "drift": node("drift", "NetLab - Check Config Drift"),
        "approval": node("approval"),
        "remediate": node("remediate", "NetLab - Configure Devices"),
        "recheck": node("recheck", "NetLab - Check Config Drift"),
    }
    api("POST", f"{C}/workflow_job_template_nodes/{n['approval']}/create_approval_template/", {
        "name": "Put the network back to NetBox?", "timeout": 3600,
        "description": "The drift report shows what differs from NetBox. Approve to overwrite it.",
    })
    for parent, child in [("drift", "approval"), ("approval", "remediate"), ("remediate", "recheck")]:
        api("POST", f"{C}/workflow_job_template_nodes/{n[parent]}/success_nodes/", {"id": n[child]})
    print("  + workflow nodes")
    return wf


def rbac(jts, wf_ids, inv, secrets_out):
    """Team "Network Ops": can run the safe, self-service jobs and see the
    inventory, but can't change templates or push arbitrary config."""
    print("RBAC: Network Ops")
    gw_org = find(f"{G}/organizations/", ORG)
    team, _ = ensure(f"{G}/teams/", "Network Ops", {
        "organization": gw_org["id"], "description": "NetLab demo: self-service network operations"})
    users = api("GET", f"{G}/users/?username=netops")["results"]
    if users:
        user = users[0]
        print("  = users: netops (password kept)")
    else:
        password = secrets.token_urlsafe(12)
        user = api("POST", f"{G}/users/", {"username": "netops", "password": password,
                                           "first_name": "Network", "last_name": "Operator"})
        print("  + users: netops")
        if secrets_out:
            path = os.path.expanduser(secrets_out)
            data = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
            data.update({"aap_netops_user": "netops", "aap_netops_password": password})
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            print(f"  netops password written to {path}")
    # Team membership is the gateway's "Team Member" role on the team.
    groles = {r["name"]: r["id"] for r in api("GET", f"{G}/role_definitions/?page_size=200")["results"]}
    have = api("GET", f"{G}/role_user_assignments/?user={user['id']}&page_size=200")["results"]
    if not any(a["role_definition"] == groles["Team Member"] and str(a["object_id"]) == str(team["id"])
               for a in have):
        api("POST", f"{G}/role_user_assignments/", {"role_definition": groles["Team Member"],
                                                     "user": user["id"], "object_id": team["id"]})
        print("  + netops in Network Ops")
    # What the team may do in automation controller.
    croles = {r["name"]: r["id"] for r in api("GET", f"{C}/role_definitions/?page_size=200")["results"]}
    grants = [("JobTemplate Execute", jts[name]["id"]) for name in (
        "NetLab - Check Config Drift", "NetLab - Backup Configs", "NetLab - Validate Fabric")]
    grants += [("WorkflowJobTemplate Execute", wf_id) for wf_id in wf_ids]
    grants += [("Inventory Use", inv["devices"])]
    team_aid = team["summary_fields"]["resource"]["ansible_id"]
    have = api("GET", f"{C}/role_team_assignments/?team_ansible_id={team_aid}&page_size=200")["results"]
    for role, obj in grants:
        if any(a["role_definition"] == croles[role] and str(a["object_id"]) == str(obj) for a in have):
            continue
        api("POST", f"{C}/role_team_assignments/", {"role_definition": croles[role],
                                                     "team_ansible_id": team_aid, "object_id": obj})
        print(f"  + {role} on {obj}")
    return team


def eda_project(org_id):
    proj, created = ensure(f"{E}/projects/", "NetLab Automation", {
        "organization_id": org_id, "url": REPO_URL, "scm_branch": BRANCH,
        "description": "NetBox -> EDA rulebooks",
    }, update=False)
    if not created:
        api("POST", f"{E}/projects/{proj['id']}/sync/")
        time.sleep(3)
    return wait(f"{E}/projects/{proj['id']}/", "import_state", {"completed"}, {"failed"})


def ensure_activation(org_id, proj, name, rulebook, eda_creds, description, stream=None, recreate=False):
    """Create the activation, or recreate it if its rulebook changed.

    With `stream`, the rulebook's first source is mapped to that event stream.
    """
    rb = find(f"{E}/rulebooks/", rulebook, project_id=proj["id"])
    if rb is None:
        raise ApiError(f"rulebook {rulebook} not found in EDA project")
    rb = api("GET", f"{E}/rulebooks/{rb['id']}/")
    mapping = None
    if stream is not None:
        src = api("GET", f"{E}/rulebooks/{rb['id']}/sources/")["results"][0]
        mapping = (f"- source_name: {src['name']}\n"
                   f"  event_stream_name: {stream['name']}\n"
                   f"  event_stream_id: {stream['id']}\n"
                   f"  rulebook_hash: {src['rulebook_hash']}\n")

    act = find(f"{E}/activations/", name)
    if act is not None:
        # The list view omits source_mappings and rulesets; use the detail view.
        act = api("GET", f"{E}/activations/{act['id']}/")
        same_rulebook = (act.get("rulebook_id") or (act.get("rulebook") or {}).get("id")) == rb["id"]
        # Event-stream activations carry the rulebook hash in their source
        # mapping; others don't expose their rulebook content, so for those
        # only the rulebook itself is compared (use --recreate-activations to
        # pick up edited rulebook content).
        unchanged = mapping is None or (
            [m.get("rulebook_hash") for m in yaml_list(act.get("source_mappings"))]
            == [yaml_list(mapping)[0]["rulebook_hash"]])
        if not recreate:
            if same_rulebook and unchanged:
                print(f"  = activation: {name}")
            else:
                # Never take a live activation down implicitly.
                print(f"  ! activation: {name} differs from {rulebook}; left running. "
                      f"Rerun with --recreate-activations to replace it.")
            return act
        # Activations are immutable; recreate when the rulebook changed.
        # Deletion is asynchronous, so wait for the name to be free.
        api("POST", f"{E}/activations/{act['id']}/disable/")
        api("DELETE", f"{E}/activations/{act['id']}/")
        end = time.time() + 300
        while find(f"{E}/activations/", name) is not None:
            if time.time() > end:
                raise ApiError(f"old activation {name} still present after 5 minutes")
            time.sleep(5)
    body = {
        "name": name, "description": description,
        "organization_id": org_id, "project_id": proj["id"], "rulebook_id": rb["id"],
        "decision_environment_id": find(f"{E}/decision-environments/", DE)["id"],
        "eda_credentials": eda_creds,
        "restart_policy": "always", "log_level": "info", "is_enabled": True,
    }
    if mapping is not None:
        body["source_mappings"] = mapping
    act = api("POST", f"{E}/activations/", body)
    print(f"  + activation: {name}")
    return act


def aws_event_path(org_id, eda_org_id, stack):
    """Credentials for the NetBox -> AWS -> SQS -> EDA path (aws/netbox-events.yml)."""
    print("AWS event path credentials")

    def aws(*args):
        out = subprocess.run(["aws", "--region", "us-east-2", "--output", "json", *args],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    outputs = {o["OutputKey"]: o["OutputValue"] for o in
               aws("cloudformation", "describe-stacks", "--stack-name", stack)["Stacks"][0]["Outputs"]}
    hmac_secret = aws("secretsmanager", "get-secret-value",
                      "--secret-id", outputs["WebhookSecretArn"])["SecretString"]
    reader = json.loads(aws("secretsmanager", "get-secret-value",
                            "--secret-id", outputs["EdaReaderSecretArn"])["SecretString"])

    ingest_ct, _ = ensure(f"{C}/credential_types/", "AWS Event Ingest", {
        "kind": "cloud",
        "description": "HTTPS endpoint + HMAC secret for sending NetBox webhooks into AWS",
        "inputs": {"fields": [
            {"id": "url", "label": "Ingest URL", "type": "string"},
            {"id": "secret", "label": "Webhook HMAC secret", "type": "string", "secret": True},
        ], "required": ["url", "secret"]},
        "injectors": {"extra_vars": {"aws_event_ingest_url": "{{ url }}",
                                     "aws_event_ingest_secret": "{{ secret }}"}},
    }, update=False)   # types can't be modified once credentials use them
    ingest, _ = ensure(f"{C}/credentials/", "NetLab AWS Event Ingest", {
        "credential_type": ingest_ct["id"], "organization": org_id,
        "description": f"API Gateway endpoint of stack {stack}",
        "inputs": {"url": outputs["WebhookUrl"], "secret": hmac_secret},
    })

    sqs_ct, _ = ensure(f"{E}/credential-types/", "AWS SQS Reader", {
        "description": "Access key for the ansible.eda.aws_sqs_queue source",
        "inputs": {"fields": [
            {"id": "access_key", "label": "Access key ID", "type": "string"},
            {"id": "secret_key", "label": "Secret access key", "type": "string", "secret": True},
        ], "required": ["access_key", "secret_key"]},
        "injectors": {"extra_vars": {"sqs_access_key": "{{ access_key }}",
                                     "sqs_secret_key": "{{ secret_key }}"}},
    }, update=False)
    sqs, _ = ensure(f"{E}/eda-credentials/", "NetLab SQS Reader", {
        "credential_type_id": sqs_ct["id"], "organization_id": eda_org_id,
        "description": f"Reads queue {outputs['QueueName']}",
        "inputs": reader,
    })
    return ingest, sqs


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--secrets-out", help="write generated NetBox admin credentials here (JSON)")
    p.add_argument("--skip-eda", action="store_true", help="controller objects only")
    p.add_argument("--netbox-events-stack", default="netlab-netbox-events",
                   help="CloudFormation stack of the NetBox -> AWS path ('' to skip)")
    p.add_argument("--recreate-activations", action="store_true",
                   help="replace the activations now, e.g. after editing a rulebook (brief EDA outage)")
    a = p.parse_args()
    if not BASE or not TOKEN:
        sys.exit("Set AAP_URL and AAP_TOKEN")

    org_id = find(f"{C}/organizations/", ORG)["id"]
    eda_org_id = find(f"{E}/organizations/", ORG)["id"]
    netbox_ct, stream_ct, login_ct, repo_ct = credential_types()
    nb_cred, dev_cred = controller_credentials(org_id, netbox_ct, a.secrets_out)
    aap_cred = stream = None
    if not a.skip_eda:
        aap_cred, stream = eda_side(eda_org_id, stream_ct)
    stream_target = find(f"{C}/credentials/", "NetLab EDA Event Stream")
    ingest = sqs_cred = None
    if a.netbox_events_stack and not a.skip_eda:
        ingest, sqs_cred = aws_event_path(org_id, eda_org_id, a.netbox_events_stack)

    hub_push, netlab_ee = hub_registry(org_id, login_ct)
    backup_cred = backup_credential(org_id, repo_ct)
    syslog_stream = None
    if not a.skip_eda:
        print("EDA event stream: device syslog")
        eda_cts = {c["name"]: c["id"] for c in api("GET", f"{E}/credential-types/?page_size=200")["results"]}
        syslog_stream = event_stream(eda_org_id, eda_cts, stream_ct, org_id, "NetLab Device Syslog",
                                     "NetLab Syslog Forwarder", "NetLab Syslog Stream",
                                     "Lab host syslog forwarder -> EDA event stream NetLab Device Syslog")
    syslog_target = find(f"{C}/credentials/", "NetLab Syslog Stream")
    proj = project(org_id)
    infra, devices = inventories(org_id, proj, find(f"{C}/credentials/", AWS_CRED)["id"], nb_cred)
    inv = {"local": find(f"{C}/inventories/", LOCAL_INVENTORY)["id"], "infra": infra["id"],
           "devices": devices["id"]}
    creds = {"aws": find(f"{C}/credentials/", AWS_CRED)["id"],
             "ssh": find(f"{C}/credentials/", SSH_CRED)["id"],
             "netbox": nb_cred["id"], "devices": dev_cred["id"],
             "stream": stream_target["id"] if stream_target else None,
             "ingest": ingest["id"] if ingest else None, "hub_push": hub_push["id"],
             "backup": backup_cred["id"], "syslog_stream": syslog_target["id"] if syslog_target else None}
    if creds["stream"] is None:
        sys.exit("NetLab EDA Event Stream credential missing; run without --skip-eda first")
    jts = job_templates(org_id, proj, find(f"{C}/execution_environments/", EE)["id"], netlab_ee["id"], inv, creds)
    wf = workflow(org_id, jts)
    vlan_wf = vlan_workflow(org_id, jts)
    remediate_workflow(org_id, jts)
    sn_cred = servicenow_side(org_id, proj, netlab_ee["id"], devices["id"])
    linux_side(org_id, proj, creds, sn_cred)
    rbac(jts, [vlan_wf["id"]], inv, a.secrets_out)
    if not a.skip_eda:
        print("EDA project + rulebook activations")
        eproj = eda_project(eda_org_id)
        ensure_activation(eda_org_id, eproj, "NetLab NetBox Changes", "netbox_events.yml",
                          [aap_cred["id"]], "NetBox changes via event stream -> NetLab - Configure Devices",
                          stream=stream, recreate=a.recreate_activations)
        if sqs_cred:
            ensure_activation(eda_org_id, eproj, "NetLab NetBox Changes (AWS SQS)", "netbox_events_sqs.yml",
                              [aap_cred["id"], sqs_cred["id"]],
                              "NetBox changes via AWS EventBridge/SQS -> NetLab - Configure Devices",
                              recreate=a.recreate_activations)
        ensure_activation(eda_org_id, eproj, "NetLab Self-Heal", "device_syslog.yml",
                          [aap_cred["id"]], "Device changed by hand (syslog) -> NetLab - Self-Heal Device",
                          stream=syslog_stream, recreate=a.recreate_activations)

    print("\nDone.")
    print(f"  Workflow: {BASE}/execution/templates/workflow-job-template/{wf['id']}/details")
    if stream:
        print(f"  Event stream URL: {stream['url']}")


if __name__ == "__main__":
    main()
