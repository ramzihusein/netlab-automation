#!/usr/bin/env python3
"""Forward device "config changed" syslog messages to an AAP EDA event stream.

Runs on the lab host (the devices' management gateway). Listens for syslog on
UDP, keeps only configuration-change messages (%SYS-5-CONFIG_I, sent by EOS
and IOS when someone leaves config mode), and POSTs each one as JSON:

  {"device": "eos-leaf1", "source_ip": "172.20.20.21",
   "message": "%SYS-5-CONFIG_I: Configured from console by admin on vty3 (172.20.20.1)"}

The rulebook decides what to do with it (rulebooks/device_syslog.yml).

Settings come from the environment (systemd unit netlab-syslog-to-eda):
  EDA_URL, EDA_HEADER, EDA_TOKEN   event stream URL and auth header
  LISTEN                           address:port, default 172.20.20.1:514
  DEVICES                          JSON {mgmt_ip: device_name}
"""
import json
import os
import re
import socket
import ssl
import sys
import urllib.request

CONFIG_CHANGE = re.compile(r"%SYS-5-CONFIG_I:.*")

url = os.environ["EDA_URL"]
headers = {"Content-Type": "application/json", os.environ.get("EDA_HEADER", "Authorization"): os.environ["EDA_TOKEN"]}
devices = json.loads(os.environ.get("DEVICES", "{}"))
host, _, port = os.environ.get("LISTEN", "172.20.20.1:514").rpartition(":")
tls = ssl._create_unverified_context()   # the AAP gateway uses a self-signed certificate

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((host, int(port)))
print(f"listening on {host}:{port}, forwarding config changes to {url}", flush=True)

while True:
    data, (ip, _) = sock.recvfrom(8192)
    match = CONFIG_CHANGE.search(data.decode(errors="replace"))
    if not match:
        continue
    event = {"device": devices.get(ip, ip), "source_ip": ip, "message": match.group(0).strip()}
    try:
        req = urllib.request.Request(url, data=json.dumps(event).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, context=tls, timeout=10) as r:
            print(f"{event['device']}: {event['message']} -> EDA {r.status}", flush=True)
    except Exception as e:   # keep listening whatever happens
        print(f"{event['device']}: forward failed: {e}", file=sys.stderr, flush=True)
