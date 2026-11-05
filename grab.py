#!/usr/bin/env python3
"""OCI free ARM (VM.Standard.A1.Flex) acquirer -- ONE-SHOT.

Runs on a short cron from GitHub Actions. Each run performs AT MOST ONE
LaunchInstance call, prints a machine-readable verdict, and exits 0.

Why this rewrite (2026-11-05):
  The old version was a `while True` loop with sleep(25). Combined with
  timeout-minutes:25 it was killed by GitHub every single run (57/57
  `cancelled`, 0 success) and piled up LaunchInstance requests. If capacity
  ever opened, several queued requests could provision >1 A1.Flex at once and
  blow past the Always Free allowance -> real billing. That is not acceptable.

Design invariants (do NOT change without Alex's explicit OK):
  * One process = ONE launch attempt. Never more than one instance per run.
  * Never delete / terminate / detach anything. Never touch network or proxy.
  * SDK auto-retry is disabled (NoneRetryStrategy) so one call = one request.
  * If any A1.Flex already exists (any live state), do nothing and exit.
  * Always exit 0 unless the SDK cannot even initialise -> 1 (for alerting).
    A "no capacity" or "limit" verdict is a HEALTHY run, not a failure.
"""
import os
import sys
import time
import tempfile

import oci
from oci.retry import NoneRetryStrategy

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
    fd, path = tempfile.mkstemp(suffix=".conf")
    with os.fdopen(fd, "w") as f:
        f.write("[DEFAULT]\n")
        for k, v in cfg.items():
            f.write(f"{k}={v}\n")
    return oci.config.from_file(path)


def already_running(compute):
    """Return (instance_id, shape) of a live A1.Flex, or (None, None).

    On API failure return ('UNKNOWN', '') so main SKIPS this run entirely.
    Skipping is always cheaper and safer than risking a duplicate instance.
    """
    try:
        result = oci.pagination.list_call_get_all_results(
            compute.list_instances, TEN)
        for inst in result.data:
            if inst.lifecycle_state in ("RUNNING", "PROVISIONING", "STARTING"):
                if "A1" in (inst.shape or "") or inst.display_name == DISPLAY_NAME:
                    return inst.id, inst.shape
        return None, None
    except Exception as e:
        print("WARN already_running failed, skipping run:", repr(e)[:220], flush=True)
        return "UNKNOWN", ""


def try_launch(compute, ad, bv, subnet):
    """ONE LaunchInstance call. Never retries, never waits for RUNNING."""
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
        return "LAUNCHED", iid
    except oci.exceptions.ServiceError as e:
        # Full error body: needed to tell LimitExceeded / Conflict / no-capacity apart.
        print("SERVICE_ERROR status=", e.status, "code=", e.code,
              "message=", str(e.message)[:700], flush=True)
        blob = (str(e.code) + " " + str(e.message)).lower()
        if "out of host capacity" in blob or "internalerror" in blob or "capacity" in blob:
            return "NO_CAPACITY", None
        if e.status == 400 or "limitexceeded" in blob:
            return "BLOCKED_LIMIT_EXCEEDED", None
        if e.status == 409 or "conflict" in blob:
            return "BLOCKED_CONFLICT", None
        return "ERROR_OTHER", None
    except Exception as e:
        print("UNEXPECTED", repr(e)[:300], flush=True)
        return "ERROR_OTHER", None


def main():
    try:
        config = build_config()
        compute = oci.core.ComputeClient(config, retry_strategy=NoneRetryStrategy())
    except Exception as e:
        print("FATAL SDK init:", repr(e)[:300], flush=True)
        sys.exit(1)

    existing, shape = already_running(compute)
    if existing == "UNKNOWN":
        print("VERDICT=SKIP_CHECK_FAILED", flush=True)
        sys.exit(0)
    if existing:
        print("VERDICT=ALREADY_RUNNING", existing, shape, flush=True)
        sys.exit(0)

    # One launch per run. Alternate the AD each minute so both ADs get probed
    # over time, while guaranteeing exactly one LaunchInstance call per run.
    ad, bv, subnet = TARGETS[(int(time.time()) // 60) % len(TARGETS)]
    verdict, iid = try_launch(compute, ad, bv, subnet)
    print("VERDICT=" + verdict, ad, (iid or ""), flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
