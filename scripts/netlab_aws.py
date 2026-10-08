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

Ensure a launch template that enables nested virtualization (KVM inside the
VM, needed for c8000v). Supported on 8th-gen Intel types (c8i/m8i/r8i):

  netlab_aws.py nested-template --region R --name N

  -> prints {"changed": bool, "name": N}
"""
import argparse
import json

import boto3
import botocore
import botocore.config
from botocore.exceptions import ClientError


def presign(a):
    # Regional endpoint + SigV4: global-endpoint URLs for a non-us-east-1 bucket
    # get 403/redirects (especially for new buckets) with older botocore.
    s3 = boto3.client("s3", region_name=a.region,
                      endpoint_url=f"https://s3.{a.region}.amazonaws.com",
                      config=botocore.config.Config(signature_version="s3v4",
                                                    s3={"addressing_style": "virtual"}))
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


def nested_template(a):
    ec2 = boto3.client("ec2", region_name=a.region)
    members = ec2.meta.service_model.shape_for("LaunchTemplateCpuOptionsRequest").members
    if "NestedVirtualization" not in members:
        # Old botocore (e.g. the AAP 2.6 supported EE) can neither set nor read
        # the flag. Accept a template created out of band; otherwise explain.
        names = [t["LaunchTemplateName"] for t in ec2.describe_launch_templates(
            Filters=[{"Name": "launch-template-name", "Values": [a.name]}])["LaunchTemplates"]]
        if names:
            return print(json.dumps({"changed": False, "name": a.name, "verified": False}))
        raise SystemExit(
            f"botocore {botocore.__version__} predates EC2 nested virtualization. Create the "
            f"launch template once with a current AWS CLI, then rerun:\n"
            f"  aws ec2 create-launch-template --region {a.region} --launch-template-name {a.name} "
            f"--launch-template-data '{{\"CpuOptions\":{{\"NestedVirtualization\":\"enabled\"}}}}' "
            f"--tag-specifications 'ResourceType=launch-template,Tags=[{{Key=Project,Value=netlab}}]'")
    data = {"CpuOptions": {"NestedVirtualization": "enabled"}}
    try:
        current = ec2.describe_launch_template_versions(
            LaunchTemplateName=a.name, Versions=["$Latest"])["LaunchTemplateVersions"][0]
    except ClientError as e:
        if "NotFound" not in e.response["Error"]["Code"]:
            raise
        ec2.create_launch_template(LaunchTemplateName=a.name, LaunchTemplateData=data,
                                   TagSpecifications=[{"ResourceType": "launch-template",
                                                       "Tags": [{"Key": "Project", "Value": "netlab"}]}])
        return print(json.dumps({"changed": True, "name": a.name}))
    if current["LaunchTemplateData"].get("CpuOptions", {}).get("NestedVirtualization") == "enabled":
        return print(json.dumps({"changed": False, "name": a.name}))
    ver = ec2.create_launch_template_version(LaunchTemplateName=a.name, LaunchTemplateData=data)
    ec2.modify_launch_template(LaunchTemplateName=a.name,
                               DefaultVersion=str(ver["LaunchTemplateVersion"]["VersionNumber"]))
    print(json.dumps({"changed": True, "name": a.name}))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["enable", "disable", "presign", "nested-template"])
    p.add_argument("--region", required=True)
    p.add_argument("--route-table-id")
    p.add_argument("--cidr")
    p.add_argument("--instance-id")
    p.add_argument("--bucket")
    p.add_argument("--prefix", default="images/")
    p.add_argument("--expires", type=int, default=3600)
    p.add_argument("--name", default="netlab-nested-virt")
    a = p.parse_args()

    if a.action == "nested-template":
        return nested_template(a)
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
