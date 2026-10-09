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
    q = urllib.parse.urlencode(dict(name=name, **filters))
    res = api("GET", f"{path}?{q}").get("results", [])
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
    return netbox, stream


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


def job_templates(org_id, proj, ee_id, inv, creds):
    print("Job templates")
    pv = {k: PROVISION_VARS[k] for k in ("vpc_id", "route_table_id", "route53_zone_id")}
    specs = [
        ("NetLab - Provision Infra", "playbooks/01_provision_infra.yml", "local", ["aws"],
         {"extra_vars": yaml_vars(PROVISION_VARS), "ask_variables_on_launch": True}),
        ("NetLab - Configure Lab Host", "playbooks/02_configure_lab_host.yml", "infra", ["aws", "ssh"], {}),
        ("NetLab - Deploy Lab", "playbooks/03_deploy_lab.yml", "infra", ["ssh"],
         {"ask_variables_on_launch": True}),
        ("NetLab - Install NetBox", "playbooks/04_install_netbox.yml", "infra", ["ssh", "netbox"], {}),
        ("NetLab - Populate NetBox", "playbooks/05_populate_netbox.yml", "infra", ["ssh", "netbox"], {}),
        ("NetLab - Configure Devices", "playbooks/06_configure_devices.yml", "devices", ["devices"],
         {"ask_limit_on_launch": True, "ask_variables_on_launch": True}),
        ("NetLab - Configure NetBox Webhook", "playbooks/07_configure_netbox_webhook.yml", "infra",
         ["ssh", "netbox", "stream", "ingest"], {"survey_enabled": True}),
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
    api("POST", f"{C}/job_templates/{jts['NetLab - Configure NetBox Webhook']['id']}/survey_spec/", {
        "name": "Event path", "description": "",
        "spec": [{
            "question_name": "NetBox event path",
            "question_description": "keep = leave the active path as is (used by the Start workflow)",
            "variable": "netbox_event_path", "type": "multiplechoice",
            "choices": ["keep", "direct", "aws"], "default": "keep", "required": True,
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
    if api("GET", nodes_path)["count"]:
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
    ]
    node = {}
    for ident, jt, converge in order:
        node[ident] = api("POST", nodes_path, {
            "unified_job_template": jts[jt]["id"], "identifier": ident,
            "all_parents_must_converge": converge})["id"]
    for parent, child in [("provision", "labhost"), ("provision", "netbox"), ("labhost", "deploy"),
                          ("deploy", "populate"), ("netbox", "populate"), ("populate", "configure"),
                          ("configure", "webhook")]:
        api("POST", f"{C}/workflow_job_template_nodes/{node[parent]}/success_nodes/", {"id": node[child]})
    print("  + workflow nodes")
    return wf


def eda_project(org_id):
    proj, created = ensure(f"{E}/projects/", "NetLab Automation", {
        "organization_id": org_id, "url": REPO_URL, "scm_branch": BRANCH,
        "description": "NetBox -> EDA rulebooks",
    }, update=False)
    if not created:
        api("POST", f"{E}/projects/{proj['id']}/sync/")
        time.sleep(3)
    return wait(f"{E}/projects/{proj['id']}/", "import_state", {"completed"}, {"failed"})


def ensure_activation(org_id, proj, name, rulebook, eda_creds, description, stream=None):
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
        if mapping is not None:
            unchanged = ([m.get("rulebook_hash") for m in yaml_list(act.get("source_mappings"))]
                         == [yaml_list(mapping)[0]["rulebook_hash"]])
        else:
            unchanged = act.get("rulebook_rulesets") == rb.get("rulesets")
        if same_rulebook and unchanged:
            print(f"  = activation: {name}")
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
        "restart_policy": "on-failure", "log_level": "info", "is_enabled": True,
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
    a = p.parse_args()
    if not BASE or not TOKEN:
        sys.exit("Set AAP_URL and AAP_TOKEN")

    org_id = find(f"{C}/organizations/", ORG)["id"]
    eda_org_id = find(f"{E}/organizations/", ORG)["id"]
    netbox_ct, stream_ct = credential_types()
    nb_cred, dev_cred = controller_credentials(org_id, netbox_ct, a.secrets_out)
    aap_cred = stream = None
    if not a.skip_eda:
        aap_cred, stream = eda_side(eda_org_id, stream_ct)
    stream_target = find(f"{C}/credentials/", "NetLab EDA Event Stream")
    ingest = sqs_cred = None
    if a.netbox_events_stack and not a.skip_eda:
        ingest, sqs_cred = aws_event_path(org_id, eda_org_id, a.netbox_events_stack)

    proj = project(org_id)
    infra, devices = inventories(org_id, proj, find(f"{C}/credentials/", AWS_CRED)["id"], nb_cred)
    inv = {"local": find(f"{C}/inventories/", LOCAL_INVENTORY)["id"], "infra": infra["id"],
           "devices": devices["id"]}
    creds = {"aws": find(f"{C}/credentials/", AWS_CRED)["id"],
             "ssh": find(f"{C}/credentials/", SSH_CRED)["id"],
             "netbox": nb_cred["id"], "devices": dev_cred["id"],
             "stream": stream_target["id"] if stream_target else None,
             "ingest": ingest["id"] if ingest else None}
    if creds["stream"] is None:
        sys.exit("NetLab EDA Event Stream credential missing; run without --skip-eda first")
    jts = job_templates(org_id, proj, find(f"{C}/execution_environments/", EE)["id"], inv, creds)
    wf = workflow(org_id, jts)
    if not a.skip_eda:
        print("EDA project + rulebook activations")
        eproj = eda_project(eda_org_id)
        ensure_activation(eda_org_id, eproj, "NetLab NetBox Changes", "netbox_events.yml",
                          [aap_cred["id"]], "NetBox changes via event stream -> NetLab - Configure Devices",
                          stream=stream)
        if sqs_cred:
            ensure_activation(eda_org_id, eproj, "NetLab NetBox Changes (AWS SQS)", "netbox_events_sqs.yml",
                              [aap_cred["id"], sqs_cred["id"]],
                              "NetBox changes via AWS EventBridge/SQS -> NetLab - Configure Devices")

    print("\nDone.")
    print(f"  Workflow: {BASE}/execution/templates/workflow-job-template/{wf['id']}/details")
    if stream:
        print(f"  Event stream URL: {stream['url']}")


if __name__ == "__main__":
    main()
