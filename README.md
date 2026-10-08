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

Then rerun **NetLab - Build Environment**. Nodes whose OS has no image are
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
| Inventory | NetLab Infra | `aws_ec2`, tag `Project=netlab`, groups `role_labhost`, `role_netbox` |
| Inventory | NetLab Devices | NetBox (`inventories/netbox.yml`), update on launch |
| Workflow | NetLab - Build Environment | 01 → (02 → 03, 04) → 05 → 06 → 07 |
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
| 05 | Populate NetBox | site, roles, platforms, devices, interfaces, IPs, cables (create-only); verify NetBox → devices |
| 06 | Configure Devices | NetBox intent → hostname, interface description/state/IPv4 (`replaced`) |
| 07 | Configure NetBox Webhook | NetBox webhook + event rule → EDA event stream; verify NetBox → AAP |
| 99 | Teardown | route, DNS, instances, SGs (images kept unless asked) |

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
