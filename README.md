# netlab-automation

Network automation demo for the AAP 2.6 environment in AWS us-east-2:
**containerlab** (Arista cEOS + Cisco IOS) and **NetBox**, with every device
reachable from both AAP and NetBox, and NetBox driving **Event-Driven Ansible**.

```
 VPC 10.0.0.0/16                         route 172.20.20.0/24 -> lab host ENI
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  aap-exec-1/2 ─── ssh ───┐                                               │
 │                          ▼                                               │
 │                  netlab-lab-host (m8i.2xlarge + nested virt, src/dst off)│
 │                    br-netlab 172.20.20.0/24  (docker nat-unprotected)    │
 │                     ├─ eos-spine1 .11   ├─ ios-spine2 .12                │
 │                     └─ eos-leaf1  .21   └─ ios-leaf2  .22                │
 │                          ▲                                               │
 │  netlab-netbox ─── ssh ──┘   (t3.large, netbox-docker :8000)             │
 │     │  webhook (Authorization token)                                     │
 │     ▼                                                                    │
 │  AAP gateway :443 ─► EDA event stream "NetLab NetBox"                    │
 │                       ─► activation "NetLab NetBox Changes"              │
 │                            ─► JT "NetLab - Configure Devices" limit=<dev>│
 └──────────────────────────────────────────────────────────────────────────┘
```

## How device reachability works

Containerlab puts device management interfaces on a Docker bridge that is
normally private to the host. Here:

1. The bridge (`netlab-mgmt`, `172.20.20.0/24`) is created by us with
   `gateway_mode_ipv4=nat-unprotected`, so Docker 28+ still NATs device traffic
   out but no longer drops traffic coming *in* to container IPs.
2. `DOCKER-USER` rules (systemd unit `netlab-forward`) allow uplink <-> bridge.
3. The VPC route table sends `172.20.20.0/24` to the lab host's ENI and the
   instance's source/dest check is off (`scripts/netlab_aws.py`).
4. `netlab-sg` lets the AAP security groups and `netbox-sg` in on all ports.

Result: AAP execution nodes and NetBox connect straight to `172.20.20.x`.
(AWS doesn't support macvlan, so this routed design is the clean option.)

## Images (you supply these)

| OS | Where to get it | Upload as |
|---|---|---|
| Arista cEOS | arista.com → Software Downloads → cEOS-lab (free account) | `cEOS64-lab-<ver>.tar.xz` |
| Cisco IOL (recommended) | CML refplat ISO → `x86_64_crb_linux-adventerprisek9-ms.iol` | any `*.iol` / `*iol*.bin` (L3 image) |
| Cisco Catalyst 8000V | software.cisco.com, non-EFI serial build | `c8000v-universalk9_8G_serial.<ver>.qcow2` |

The c8000v is a VM inside its container, so the lab host needs `/dev/kvm`. The
default lab host type, `m8i.2xlarge`, is launched from a launch template with
EC2 nested virtualization enabled (8th-gen Intel c8i/m8i/r8i only), which costs
about a tenth of a `.metal` instance. To change the type, rerun **Provision
Infra** with `lab_instance_type=<type> lab_replace=true`.

The AAP 2.6 supported EE ships botocore 1.34, which predates the nested
virtualization API, so create the launch template once with a current AWS CLI
(Provision Infra prints this command if it's missing):

```bash
aws ec2 create-launch-template --region us-east-2 --launch-template-name netlab-nested-virt \
  --launch-template-data '{"CpuOptions":{"NestedVirtualization":"enabled"}}' \
  --tag-specifications 'ResourceType=launch-template,Tags=[{Key=Project,Value=netlab}]'
```

```bash
aws s3 cp cEOS64-lab-4.xx.tar.xz            s3://aap-netlab-images-<account>/images/
aws s3 cp x86_64_crb_linux-adventerprisek9-ms.iol s3://aap-netlab-images-<account>/images/
```

Then rerun **NetLab - Start (build if needed)**. Nodes whose OS has no image are
skipped, so the lab works with only cEOS, only IOS, or both. If both IOL and
c8000v are present, IOL is used (`ios_kind_preference`).

## AAP objects

Created by `bootstrap/aap_bootstrap.py` (idempotent):

| Kind | Name | Notes |
|---|---|---|
| Project | NetLab Automation | this repo |
| Credential type | NetBox API | env `NETBOX_API`/`NETBOX_TOKEN` + `netbox_*` vars |
| Credential type | EDA Event Stream Target | `eda_event_stream_*` vars |
| Credential | NetLab NetBox | URL `http://netbox-int.aap.ramzidev.com:8000` |
| Credential | NetLab Devices | `admin` / `admin` (containerlab defaults, VPC-only) |
| Credential | NetLab EDA Event Stream | event stream URL + token for the NetBox webhook |
| Credential type | Container Registry Login | `ee_registry*` vars (build job) |
| Credential | NetLab Hub Registry / NetLab Hub Push | automation hub login (AAP admin); type the password in the AAP UI once |
| Execution environment | NetLab EE | `ee/execution-environment.yml`, used by Populate NetBox |
| Inventory | NetLab Infra | `aws_ec2`, tag `Project=netlab`, groups `role_labhost`, `role_netbox` |
| Inventory | NetLab Devices | NetBox (`inventories/netbox.yml`), update on launch |
| Workflow | NetLab - Start (build if needed) | 01 → (02 → 03, 04) → 05 → 06 → 07 |
| Job template | NetLab - Teardown | survey: type `netlab` to confirm |
| EDA | NetLab AAP, NetLab NetBox Webhook, event stream NetLab NetBox, activation NetLab NetBox Changes | |

```bash
AAP_URL=https://gateway.aap.ramzidev.com AAP_TOKEN=<token> \
  python3 bootstrap/aap_bootstrap.py --secrets-out ~/.netlab-secrets.json
```

## Playbooks

| # | Job template | What it does |
|---|---|---|
| 01 | Provision Infra | S3 image bucket, SGs, lab + NetBox EC2, VPC route, DNS |
| 02 | Configure Lab Host | Docker, containerlab, routed mgmt bridge, import/build images |
| 03 | Deploy Lab | render `topology.clab.yml`, deploy, verify SSH to every device from AAP |
| 04 | Install NetBox | netbox-docker 3.3.0 (NetBox 4.3), admin user, AAP API token |
| 05 | Populate NetBox | everything in `vars/netlab.yml` with the `netbox.netbox` modules (lab objects create-only); verify NetBox → devices. Runs on **NetLab EE** |
| 06 | Configure Devices | NetBox intent → hostname, interface description/state/IPv4 (`replaced`) |
| 07 | Configure NetBox Webhook | NetBox webhook + event rule → EDA event stream; verify NetBox → AAP |
| 09 | Build Execution Environment | ansible-builder v3 on the lab host → push `netlab-ee` to the automation hub |
| 99 | Teardown | route, DNS, instances, SGs (images kept unless asked) |

## Execution environment (`ee/`)

`ee/execution-environment.yml` is an **ansible-builder v3** definition
(`version: 3`): the supported AAP 2.6 EE (`ee-supported-rhel9`, pulled from
the automation hub that ships with AAP, so no registry.redhat.io login is
needed) plus the `netbox.netbox` collection and `pynetbox`. Only Populate
NetBox needs it; everything else stays on the default EE.

To change it, edit the files in `ee/`, push, and run **NetLab - Build
Execution Environment**. It builds on the lab host, checks the collection and
pynetbox are in the image, and pushes `netlab-ee:latest` plus a dated tag
(e.g. `netlab-ee:20261013-0201`) for rollback. AAP pulls it as
`ip-10-0-1-147.us-east-2.compute.internal/netlab-ee` (the gateway's
certificate is issued for node names, which the execution nodes trust).

Build by hand (anywhere with podman):

```bash
pip install 'ansible-builder>=3.1,<4'
cd ee && ansible-builder build -t netlab-ee:latest --extra-build-cli-args=--tls-verify=false
```

## Demo: NetBox drives the network

1. Open NetBox (`http://netbox.aap.ramzidev.com:8000`), go to **eos-leaf1 → Interfaces → Ethernet1**.
2. Change the description, or disable the interface, or change its IP.
3. NetBox fires the webhook → EDA activation **NetLab NetBox Changes** →
   **NetLab - Configure Devices** runs with `limit=eos-leaf1`.
4. `ssh admin@172.20.20.21` from the lab host (or check the job output) to see the change.

## Topology

Edit `vars/netlab.yml` to add nodes/links before first population. After that,
NetBox is the source of truth; `05` never overwrites devices, interfaces or IPs
that already exist.

## Notes

- Stopping/starting the lab host: rerun **NetLab - Deploy Lab** (it redeploys
  missing nodes) then **NetLab - Configure Devices** (NetBox is the source of truth).
  NetBox restarts by itself (`netbox.service`).
- The AAP gateway uses a self-signed certificate, so the NetBox webhook has
  SSL verification off.
- Device passwords are the containerlab defaults; the mgmt network is only
  reachable from inside the VPC.

## Power schedule (AAP + NetLab)

`aws/aap-power-schedule.yml` (CloudFormation stack `aap-lab-power-schedule`) keeps
AAP off outside working hours. Instances opt in with the tag `PowerSchedule`:
`aap` = started and stopped by the schedule, `stop-only` = stopped in the evening
but started on demand (NetLab).

| | On | Off |
|---|---|---|
| AAP | 07:00 ET Mon–Fri, or Step Functions → `aap-lab-start` → Start execution | 19:00 ET Mon–Fri, or `aap-lab-stop` |
| NetLab | AAP workflow **NetLab - Start (build if needed)** | AAP job **NetLab - Stop**, and every weekday evening with AAP |

`aap-lab-start` starts the RDS database first, then the instances, and only
succeeds once the gateway, controller (all nodes with fresh heartbeats), EDA and
hub are healthy; otherwise it fails after ~20 minutes with the failing checks.
An EventBridge rule re-stops the database if RDS auto-starts it after 7 days.

Measured on 2026-10-09: stop 34 s; cold start 12 min (database ~8 min);
NetLab cold start 8 min.

```bash
# start / stop by hand
aws stepfunctions start-execution --region us-east-2 \
  --state-machine-arn arn:aws:states:us-east-2:863099780958:stateMachine:aap-lab-start
aws stepfunctions start-execution --region us-east-2 \
  --state-machine-arn arn:aws:states:us-east-2:863099780958:stateMachine:aap-lab-stop

# pause or resume the schedule
aws cloudformation deploy --region us-east-2 --stack-name aap-lab-power-schedule \
  --template-file aws/aap-power-schedule.yml --capabilities CAPABILITY_IAM \
  --parameter-overrides ScheduleState=DISABLED   # or ENABLED
```

## NetBox events through AWS (EventBridge + SQS)

A second, switchable event path for customers who want NetBox events on their
AWS event backbone before they reach EDA:

```
NetBox webhook (HMAC-signed) ─► API Gateway ─► Lambda (verifies X-Hook-Signature)
   ─► EventBridge bus netlab-netbox-events (30-day archive, replayable)
   ─► rule: source=netbox, model in [device, interface, ipaddress], InputPath $.detail
   ─► SQS FIFO netlab-netbox-events-eda.fifo (content-based dedup, 4-day retention, DLQ)
   ─► EDA activation "NetLab NetBox Changes (AWS SQS)" (ansible.eda.aws_sqs_queue)
   ─► "NetLab - Configure Devices" limit=<device>
```

| Piece | Where |
|---|---|
| AWS resources | `aws/netbox-events.yml` (stack `netlab-netbox-events`) |
| Rulebook | `rulebooks/netbox_events_sqs.yml` (same rules as the direct one, `event.body` instead of `event.payload`) |
| Read the events | CloudWatch → Log groups → `/aws/events/netlab-netbox-events` (every event on the bus, 30 days) |
| Credentials | AAP "NetLab AWS Event Ingest" (URL + HMAC secret), EDA "NetLab SQS Reader" (queue-only access key) |

**Switching paths:** launch **NetLab - Configure NetBox Webhook** and choose
`direct` or `aws` (`keep`, the default, leaves it as is; the Start workflow uses
that). NetBox has a single webhook, so exactly one activation receives events;
the other idles. Verified: one NetBox edit → one job, on either path.

Notes for customer conversations:
- The AWS path keeps events while AAP is down (SQS holds them 4 days) and lets
  other consumers subscribe on the bus; the direct path is simpler.
- The default AAP 2.6 decision environment already includes the SQS source.
- EventBridge API destinations (push straight to an EDA event stream) need a
  publicly trusted certificate on the AAP gateway; this lab's is self-signed,
  hence the pull-from-SQS design.
- `bootstrap/aap_bootstrap.py` never replaces a running activation unless run
  with `--recreate-activations` (needed after editing a rulebook).

## The fabric (NetBox is the source of truth for all of it)

```
            eos-spine1 (AS 65000)         ios-spine2 (AS 65000)
              Et1     Et2                   Gi2     Gi3
               |  \  /                       |  \  /
               |   \/  eBGP over /31s        |   \/
               |   /\                        |   /\
              Et1  Et2                      Gi2  Gi3
            eos-leaf1 (AS 65101)          ios-leaf2 (AS 65102)
            Vlan10 WEB  172.16.10.1/24    Gi4.30 DB      172.16.30.1/24
            Vlan20 APP  172.16.20.1/24    Gi4.40 BACKUP  172.16.40.1/24
              Et3 (access 10)               Gi4 (802.1Q trunk)
               |                             |
            host1 172.16.10.10            host2 172.16.30.10 (eth1.30)
```

| Config | Lives in NetBox as | Change it → |
|---|---|---|
| BGP sessions | **Plugins → BGP → Sessions** (netbox-bgp): local/remote IP + AS, status | that device + its neighbors; **Offline** = neighbor shut down |
| BGP ASNs | **IPAM → ASNs** (referenced by the sessions) | all devices |
| Advertised networks | interfaces tagged `bgp-advertise` (loopbacks, VLAN gateways) | that device |
| ECMP (`maximum-paths`) | config context **NetLab - Routing** | all devices |
| VLANs | IPAM → VLANs, interface mode / untagged / tagged VLAN | all devices |
| Gateways | `Vlan<vid>` SVIs (EOS), `<port>.<vid>` subinterfaces (IOS) | that device |
| NTP, DNS, syslog, SNMP, banner | config context **NetLab - Global services** | all devices |
| ACLs | config context **NetLab - Security policy** | all devices |
| ACL bindings | interface tag `acl-servers-in` (bound per `acl_bindings`) | that device |

### How the device playbooks are organised

`playbooks/configure_network.yml` (jobs **Configure Devices** and **Check Config
Drift**) is just a list of roles, one per feature:

```yaml
roles:
  - netbox_scope      # which devices does this change affect?
  - base_services     # hostname, DNS, SNMP, LLDP, NTP, syslog, banner
  - vlans             # VLANs on switch ports and VLAN interfaces
  - interfaces        # descriptions, state, switchports, subinterfaces, IPs
  - acls              # access lists, applied by interface tag
  - bgp               # sessions from the NetBox BGP plugin
  - save_config
  - report
```

Each role is `tasks/main.yml` (a few lines), `tasks/eos.yml` + `tasks/ios.yml`
(one resource-module task per thing), and `templates/*.yml.j2` that look like
the data each module takes. `configure_<feature>.yml` runs a single role (jobs
**NetLab - Configure BGP / VLANs / Interfaces / ACLs / Base Services**).
Connection settings per vendor are in `playbooks/group_vars/platforms_*.yml`.

All modules use `state: replaced`, so runs are idempotent ("Already matched
NetBox") and the check-mode job reports real drift only.

cEOS-lab can't bind ACLs to interfaces (no data-plane ACLs, the command is
rejected as unsupported), so EOS bindings are only pushed with
`eos_acl_bindings=true` (real Arista / vEOS). The ACLs themselves are configured
on all devices; bindings are enforced on the Cisco c8000v leaves.

## Getting started: Ansible without NetBox

For audiences new to Ansible, before NetBox and EDA. The data is plain YAML in
`vars/intro/`, and each playbook is a few tasks using network **resource
modules**:

| Job template | Playbook | Shows |
|---|---|---|
| NetLab Intro 1 - Read Device Config as Data | `intro_1_read_config.yml` | `state: gathered`: device config returned as structured data, the same shape for both vendors. Changes nothing. |
| NetLab Intro 2 - Configure VLANs | `intro_2_configure_vlans.yml` + `vars/intro/vlans.yml` | Adds VLANs 100/110/120 on eos-leaf1. A survey picks `merged` / `replaced` / `overridden` / `deleted`. Prints the commands Ansible worked out. Run it twice: the second run changes nothing. |
| NetLab Intro 3 - Configure Interface Descriptions | `intro_3_configure_interfaces.yml` + `vars/intro/interfaces.yml` | The same data on Arista and Cisco, with two modules. Survey: `merged` / `replaced`. |
| NetLab - Check Drift and Remediate (workflow) | — | Drift report (with the commands that would fix it) → approval → Configure Devices → drift check again ("In sync with NetBox"). |

These jobs run from AAP, so self-heal ignores the changes. They stay on the
devices until you remediate. The workflow then shows NetBox as the source of
truth and resets the lab in one go.

Suggested order:
1. Intro 1.
2. Edit `vars/intro/vlans.yml` in GitHub, run Intro 2, then run it again (nothing changes).
3. Intro 3.
4. Check Drift and Remediate.
5. Now introduce NetBox: the same idea, with the data in a source of truth instead of files.

## Demo scripts

**Config drift**
1. Change something by hand, e.g. `ssh eos-spine1` → `conf` → `interface Ethernet1` → `description oops`
2. Run **NetLab - Check Config Drift** (check + diff mode, changes nothing) →
   `eos-spine1: DRIFT in: interfaces` with the exact command that would fix it
3. Run **NetLab - Configure Devices** → only that drift is corrected

**NetBox drives the network (EDA)**
- Edit a VLAN gateway description, shut an interface, or change an IP on a
  device → one job for that device **and its BGP neighbors** (the job output
  shows "Change on ios-leaf2 -> ios-leaf2, eos-spine1, ios-spine2")
- Edit the config context (add/remove an NTP server, change the banner) →
  one job for all devices; removals are removed from the devices too
- Tag another interface `acl-servers-in` → ACL bound on it (Cisco)

**BGP from NetBox**
- Plugins → BGP → Sessions → `eos-leaf1 to ios-spine2` → Status **Offline** →
  EDA runs Configure Devices for eos-leaf1 (+ neighbors) → `neighbor … shutdown`,
  session goes Idle(Admin); set it back to **Active** and it re-establishes

**Traffic across the fabric**
`ssh netlab` → `sudo docker exec clab-netlab-host1 ping 172.16.30.10`
(Arista VLAN 10 → spines, ECMP → Cisco VLAN 30)

**Onboarding a device**: rebuild the lab (or a device) with no config, then run
**NetLab - Configure Devices**: everything above is rendered from NetBox.

## Day-2 operations

**Backups** (`backup_configs.yml`, job **NetLab - Backup Configs**, also
scheduled weekdays 18:45 ET before the lab stops): every device's running
config is committed to the private repo `ramzihusein/netlab-backups`
(`configs/<device>.cfg`), so the commit history is a timeline of the network.
**NetLab - Restore Configs** puts devices back to the latest backup or any
commit (survey: `backup_ref`) with `configure replace`, which also removes
lines added since.

**Self-healing** (`rulebooks/device_syslog.yml`, activation **NetLab
Self-Heal**): devices send syslog to the lab host (NetBox config context
`syslog_hosts`), where `scripts/syslog_to_eda.py` forwards config-change
messages (`%SYS-5-CONFIG_I`) to the EDA event stream **NetLab Device Syslog**.
A change made by a person (not from an AAP execution node) runs **NetLab -
Self-Heal Device** for that device straight away, in diff mode
so the job shows what was put back. Further changes to the same device within
60 s are covered by that job (one job per device per minute).

**Request Server VLAN** (workflow, survey): backup → **Validate Fabric**
(pre-check) → **approval** → add the VLAN to NetBox (as user `aap`, so EDA
leaves it to the workflow) → **Configure Devices** → post-check (BGP up,
loopbacks reachable, spines route the new subnet) → on failure, **Restore
Configs** and a journal entry on the VLAN in NetBox. Answer "Simulate a bad
change" = yes to see the restore.

**RBAC**: team **Network Ops** (user `netops`) can run Check Config Drift,
Backup Configs, Validate Fabric and Request Server VLAN, and use the device
inventory; nothing else.
