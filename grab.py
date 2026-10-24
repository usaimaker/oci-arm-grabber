#!/usr/bin/env python3
"""OCI free ARM (VM.Standard.A1.Flex) acquirer.

Runs 24/7 from GitHub Actions. Reads OCI identity from env vars (non-secret
OCIDs / fingerprint / region) and the private key from OCI_KEY_PATH (written
from the OCI_KEY secret). Tries to launch the retained boot volumes in both
PHX ADs; exits the moment the instance is RUNNING. If the instance is already
up, it exits cleanly so the job does not spin uselessly.
"""
import os
import sys
import time
import tempfile

import oci

# ---- non-secret identifiers (safe to hardcode) ----
TEN = "ocid1.tenancy.oc1..aaaaaaaanlusktnkyxysj3dor6cqenvzs23c6v3pkseh6aghiify7yuna5mq"

# our deploy public key + Oracle's default free-vm key (both non-secret)
OUR_KEY = ("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQC3PkacsPMnEJRH4WWDt+77+u/uAciLg0yYng1IJ"
           "eywIHErGkQHB8gDUsj5sMLJFXUxb/LcM+EcLd+HIPkeMFBJh/IptBJY9siWc7J36JFWlUKT1sVQxy"
           "RT/FsDL2NQiVOFfSlqsnAU5iP2Nn1cCqIZJXK9Z6R1hH2qUC7RU0be0J0nYcn817a4bnw7FzKDQBHr"
           "vJIDfQHyQZDXVvXHN5hVY11U8rLEdHigTOf2UR5kp0y0BX0hPZFNZ7XvvXRvbRXTVrNo3kHP3tRek6"
           "Qn1DzfXQry2hBgGYSIyQTIR0ejaSRZfweSyGhmS3I/R+2BxaPLPW769ASGQZ4m6IlE/kW6TCongILpc"
           "HyVup6ZYfYlqMmun5bDn1VYmy9Zu0O5C39ITiZWQ4d2R9UODVC40hTKx1wzypXnJCpvWNvkJxGbYIWN"
           "PzHgFilzWZ63PLIM0bOb66GbG8ysdB2YgYptPMpZLuzIe4VYsORxOUD74xPucw33csnrXeRfbM7zWP0"
           "S6o70nmlm4blaooTO1TCTC0Zqz4bW1KKoUwmqi/XitMyJtFRU8JW7b+6ZuAKsOeeWN2byhmw7Wbaxyj"
           "TmtnA5EncVEoOHZ1tvq6jTvj9aogxRORtRq/bLRhdJPcZ/9ae+w91yHAJdQUEf2+qTlttDI9Y2jrP5cl"
           "iiDbrPVRU66pSL9w== lnhys-deploy")
ORACLE_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKKJ4ZlpLC67siUJUwhUwiWVJeVEG19vbhAsmyPB1qGa oracle-free-vm"

# (availability_domain, boot_volume_id, subnet_id) -- retained boot volumes
TARGETS = [
    ("XNQJ:PHX-AD-1",
     "ocid1.bootvolume.oc1.phx.abyhqljsppgbsqjabnbzqzdoyzv4jjjicwslu6il2oofnbgr3xlcbeqidtoa",
     "ocid1.subnet.oc1.phx.aaaaaaaa5f2mocmknunvkh4wgctymax2y4u22qvcbntb6p27vd6dhtqb2qpa"),
    ("XNQJ:PHX-AD-2",
     "ocid1.bootvolume.oc1.phx.abyhqljt2o37dg5szz4icvbvhz6ngjxuvmj7z5h5ioe3voeaeuq3evrzgkta",
     "ocid1.subnet.oc1.phx.aaaaaaaa5ztyaeoi65st7cwjk2c2xo3vbktpl7d4pny7qe73knh6gnb53tcq"),
]

DISPLAY_NAME = "free-arm-biggest"


def build_config():
    cfg = {
        "user": os.environ["OCI_USER"],
        "fingerprint": os.environ["OCI_FINGERPRINT"],
        "tenancy": os.environ["OCI_TENANCY"],
        "region": os.environ["OCI_REGION"],
        "key_file": os.environ.get("OCI_KEY_PATH", "/home/runner/oci_key.pem"),
    }
    # oci.config.from_file wants INI; write a temp one from the dict.
    fd, path = tempfile.mkstemp(suffix=".conf")
    with os.fdopen(fd, "w") as f:
        f.write("[DEFAULT]\n")
        for k, v in cfg.items():
            f.write(f"{k}={v}\n")
    return oci.config.from_file(path)


def already_running(compute):
    try:
        result = oci.pagination.list_call_get_all_results(
            compute.list_instances, TEN, display_name=DISPLAY_NAME)
        for inst in result.data:
            if inst.lifecycle_state in ("RUNNING", "PROVISIONING", "STARTING"):
                return inst.id
    except Exception as e:
        print("already_running check err:", repr(e)[:140], flush=True)
    return None


def try_launch(compute, ad, bv, subnet):
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad,
        compartment_id=TEN,
        shape="VM.Standard.A1.Flex",
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=2, memory_in_gbs=12),
        source_details=oci.core.models.InstanceSourceViaBootVolumeDetails(
            source_type="bootVolume", boot_volume_id=bv),
        subnet_id=subnet,
        display_name=DISPLAY_NAME,
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet, assign_public_ip=False),
        metadata={"ssh_authorized_keys": OUR_KEY + "\n" + ORACLE_KEY},
    )
    try:
        resp = compute.launch_instance(details)
        iid = resp.data.id
        print("LAUNCHED in", ad, iid, flush=True)
        for _ in range(50):
            st = compute.get_instance(iid).data.lifecycle_state
            if st == "RUNNING":
                print("RUNNING", iid, flush=True)
                return iid
            if st in ("TERMINATED", "FAILED"):
                print("BAD STATE", st, flush=True)
                return None
            time.sleep(8)
        print("TIMEOUT waiting RUNNING", flush=True)
        return None
    except Exception as e:
        msg = repr(e)
        if "Out of host capacity" in msg or "InternalError" in msg:
            print("capacity busy in", ad, flush=True)
        else:
            print("err in", ad, msg[:160], flush=True)
        return None


def main():
    config = build_config()
    compute = oci.core.ComputeClient(config)
    attempt = 0
    while True:
        attempt += 1
        existing = already_running(compute)
        if existing:
            print("ALREADY_RUNNING", existing, flush=True)
            sys.exit(0)
        for (ad, bv, subnet) in TARGETS:
            iid = try_launch(compute, ad, bv, subnet)
            if iid:
                print("SUCCESS_INSTANCE_ID=" + iid, flush=True)
                sys.exit(0)
        if attempt % 10 == 0:
            print("heartbeat round", attempt, "still trying (free ARM capacity)", flush=True)
        time.sleep(25)


if __name__ == "__main__":
    main()
