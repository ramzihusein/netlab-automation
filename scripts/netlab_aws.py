#!/usr/bin/env python3
"""AWS helpers for the NetLab playbooks that amazon.aws doesn't cover cleanly.

Route the containerlab management subnet through the lab host. amazon.aws has
no single-route module (ec2_vpc_route_table manages the whole table), so this
makes only the calls we need and leaves every other route alone:

  netlab_aws.py enable  --region R --route-table-id T --cidr C --instance-id I
  netlab_aws.py disable --region R --route-table-id T --cidr C

  -> prints {"changed": bool} for the calling task's changed_when.

List NOS images uploaded under a bucket prefix with presigned download URLs,
so the lab host can fetch them without an instance profile:

  netlab_aws.py presign --region R --bucket B [--prefix images/] [--expires 3600]

  -> prints [{"key", "name", "size", "url"}, ...]
"""
import argparse
import json

import boto3


def presign(a):
    s3 = boto3.client("s3", region_name=a.region)
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=a.bucket, Prefix=a.prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            if not name:
                continue
            url = s3.generate_presigned_url("get_object", ExpiresIn=a.expires,
                                            Params={"Bucket": a.bucket, "Key": obj["Key"]})
            out.append({"key": obj["Key"], "name": name, "size": obj["Size"], "url": url})
    print(json.dumps(out))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["enable", "disable", "presign"])
    p.add_argument("--region", required=True)
    p.add_argument("--route-table-id")
    p.add_argument("--cidr")
    p.add_argument("--instance-id")
    p.add_argument("--bucket")
    p.add_argument("--prefix", default="images/")
    p.add_argument("--expires", type=int, default=3600)
    a = p.parse_args()

    if a.action == "presign":
        if not a.bucket:
            p.error("--bucket is required for presign")
        return presign(a)
    if not (a.route_table_id and a.cidr):
        p.error("--route-table-id and --cidr are required")

    ec2 = boto3.client("ec2", region_name=a.region)
    changed = False

    table = ec2.describe_route_tables(RouteTableIds=[a.route_table_id])["RouteTables"][0]
    route = next((r for r in table["Routes"] if r.get("DestinationCidrBlock") == a.cidr), None)

    if a.action == "enable":
        if not a.instance_id:
            p.error("--instance-id is required for enable")
        # The lab host forwards traffic for container IPs, so AWS must not drop
        # packets whose destination isn't the instance's own address.
        attr = ec2.describe_instance_attribute(InstanceId=a.instance_id, Attribute="sourceDestCheck")
        if attr["SourceDestCheck"]["Value"]:
            ec2.modify_instance_attribute(InstanceId=a.instance_id, SourceDestCheck={"Value": False})
            changed = True
        if route is None:
            ec2.create_route(RouteTableId=a.route_table_id, DestinationCidrBlock=a.cidr,
                             InstanceId=a.instance_id)
            changed = True
        elif route.get("InstanceId") != a.instance_id or route.get("State") == "blackhole":
            ec2.replace_route(RouteTableId=a.route_table_id, DestinationCidrBlock=a.cidr,
                              InstanceId=a.instance_id)
            changed = True
    elif route is not None:
        ec2.delete_route(RouteTableId=a.route_table_id, DestinationCidrBlock=a.cidr)
        changed = True

    print(json.dumps({"changed": changed}))


if __name__ == "__main__":
    main()
