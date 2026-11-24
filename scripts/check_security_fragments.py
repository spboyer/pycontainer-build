"""Opt-in Linux check that applies generated security fragments in real runtimes.

The OCI path builds a probe image with pycontainer, unpacks the OCI layout into a
runtime bundle, merges ``security/oci-runtime-config.fragment.json`` into the
spec produced by ``runc spec``/``crun spec``, and runs the container as uid 0
and (when possible) uid 65534. The optional Kubernetes path validates the
generated ``securityContext`` with a server-side dry run and runs a probe Pod.

Each check reports PASS, SKIP or FAIL. SKIP means the control was not verified
on this host and is never evidence that it is enforced. Exit status is 1 when
any check fails.
"""
import argparse
import json
import os
import platform as host_platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.security import (
    KUBERNETES_FRAGMENT,
    OCI_FRAGMENT,
    SECURITY_DIR,
    merge_oci_runtime_fragment,
)

CAP_NET_BIND_SERVICE_MASK = 1 << 10
EXPECTED_CAPABILITY_HEX = f"{CAP_NET_BIND_SERVICE_MASK:016x}"
SECCOMP_FILTER_MODE = "2"
EROFS = 30
EPERM = 1
NOBODY_UID = 65534
MACHINE_PLATFORMS = {"x86_64": "linux/amd64", "amd64": "linux/amd64", "aarch64": "linux/arm64", "arm64": "linux/arm64"}

PROBE_SOURCE = r'''
import json, os, socket

def status_fields():
    fields = {}
    with open("/proc/self/status") as handle:
        for line in handle:
            key, _, value = line.partition(":")
            fields[key] = value.strip()
    return fields

def attempt(action):
    try:
        action()
        return "ok"
    except OSError as error:
        return error.errno

def write_root():
    with open("/pycontainer-probe-write", "w") as handle:
        handle.write("x")

def write_shm():
    with open("/dev/shm/pycontainer-probe", "w") as handle:
        handle.write("x")

def bind_privileged_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", 80))

def read_lsm_label():
    for path in ("/proc/self/attr/apparmor/current", "/proc/self/attr/current"):
        try:
            with open(path) as handle:
                return handle.read().strip("\x00\n ")
        except OSError:
            continue
    return None

fields = status_fields()
result = {
    "uid": os.getuid(),
    "CapEff": fields.get("CapEff"),
    "CapPrm": fields.get("CapPrm"),
    "CapInh": fields.get("CapInh"),
    "CapBnd": fields.get("CapBnd"),
    "CapAmb": fields.get("CapAmb"),
    "Seccomp": fields.get("Seccomp"),
    "write_root": attempt(write_root),
    "write_shm": attempt(write_shm),
    "mkdir_shm": attempt(lambda: os.mkdir("/dev/shm/pycontainer-probe-dir")),
    "chown_shm": attempt(lambda: os.chown("/dev/shm/pycontainer-probe", 1, 1)),
    "bind_80": attempt(bind_privileged_port),
    "lsm_label": read_lsm_label(),
}
print("PYCONTAINER-PROBE " + json.dumps(result, sort_keys=True))
'''

SECCOMP_PROFILE = {
    "defaultAction": "SCMP_ACT_ALLOW",
    "syscalls": [
        {"names": ["mkdir", "mkdirat"], "action": "SCMP_ACT_ERRNO", "errnoRet": EPERM},
    ],
}


class Report:
    def __init__(self):
        self.results = []

    def record(self, status, name, detail=""):
        self.results.append((status, name, detail))
        suffix = f" - {detail}" if detail else ""
        print(f"{status:4} {name}{suffix}", flush=True)

    def check(self, name, condition, detail):
        self.record("PASS" if condition else "FAIL", name, detail)

    def skip(self, name, reason):
        self.record("SKIP", name, reason)

    @property
    def failed(self):
        return any(status == "FAIL" for status, _, _ in self.results)


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def apparmor_enabled():
    return read_text("/sys/module/apparmor/parameters/enabled") == "Y"


def selinux_enabled():
    return Path("/sys/fs/selinux/enforce").exists()


def find_runtime(requested):
    candidates = [requested] if requested else ["runc", "crun"]
    for name in candidates:
        executable = shutil.which(name)
        if executable:
            return executable
    raise SystemExit(f"required OCI runtime not found: {' or '.join(candidates)}")


def host_target_platform():
    machine = host_platform.machine().lower()
    if machine not in MACHINE_PLATFORMS:
        raise SystemExit(f"unsupported host architecture for this check: {machine}")
    return MACHINE_PLATFORMS[machine]


def read_blob(layout, digest):
    algorithm, _, value = digest.partition(":")
    return layout / "blobs" / algorithm / value


def load_json_blob(layout, digest):
    return json.loads(read_blob(layout, digest).read_text())


def remove_path(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def open_layer(path, media_type):
    if "zstd" in media_type:
        raise RuntimeError(f"zstd layers are not supported by this check: {media_type}")
    return tarfile.open(path, mode="r|*")


def contained_path(rootfs, relative):
    current = rootfs
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(
                f"layer entry {relative.as_posix()} traverses symlink "
                f"{current.relative_to(rootfs).as_posix()}; refusing to modify paths outside the rootfs"
            )
    return rootfs / relative


def apply_layer(layer_path, media_type, rootfs):
    with open_layer(layer_path, media_type) as layer:
        for member in layer:
            relative = Path(member.name.lstrip("/"))
            if any(part == ".." for part in relative.parts):
                raise RuntimeError(f"layer entry escapes rootfs: {member.name}")
            target = contained_path(rootfs, relative)
            name = relative.name
            if name == ".wh..wh..opq":
                directory = target.parent
                if directory.is_dir():
                    for child in directory.iterdir():
                        remove_path(child)
                continue
            if name.startswith(".wh."):
                remove_path(target.parent / name[len(".wh."):])
                continue
            if not member.isdir() and (target.exists() or target.is_symlink()):
                remove_path(target)
            layer.extract(member, rootfs, set_attrs=os.geteuid() == 0, filter="tar")


def unpack_image(layout, tag, rootfs):
    index = json.loads((layout / "index.json").read_text())
    manifest_descriptor = next(
        (entry for entry in index["manifests"]
         if entry.get("annotations", {}).get("org.opencontainers.image.ref.name") == tag),
        index["manifests"][0],
    )
    manifest = load_json_blob(layout, manifest_descriptor["digest"])
    image_config = load_json_blob(layout, manifest["config"]["digest"])
    rootfs.mkdir()
    for layer in manifest["layers"]:
        apply_layer(read_blob(layout, layer["digest"]), layer["mediaType"], rootfs)
    return image_config.get("config", {})


def build_probe_image(args, work, platform, security):
    context = work / "context"
    context.mkdir()
    (context / "probe.py").write_text(PROBE_SOURCE)
    (context / "seccomp.json").write_text(json.dumps(SECCOMP_PROFILE))
    layout = work / "layout"
    config = BuildConfig(
        tag="security-check",
        base_image=args.base_image,
        context_dir=str(context),
        output_dir=str(layout),
        entrypoint=["python3", "/app/probe.py"],
        platform=platform,
        use_cache=False,
        include_paths=["probe.py"],
        security=security,
    )
    ImageBuilder(config).build()
    return layout


def run_runtime(runtime, bundle, timeout, container_id):
    try:
        return subprocess.run(
            [runtime, "run", "--bundle", str(bundle), container_id],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    finally:
        subprocess.run(
            [runtime, "delete", "--force", container_id],
            capture_output=True,
            check=False,
            timeout=30,
        )


def parse_probe(output):
    for line in output.splitlines():
        if line.startswith("PYCONTAINER-PROBE "):
            return json.loads(line[len("PYCONTAINER-PROBE "):])
    return None


def check_oci_probe(report, label, probe, args):
    for field in ("CapEff", "CapPrm", "CapInh", "CapBnd", "CapAmb"):
        report.check(f"oci[{label}] {field}", probe[field] == EXPECTED_CAPABILITY_HEX,
                     f"expected {EXPECTED_CAPABILITY_HEX}, got {probe[field]}")
    report.check(f"oci[{label}] seccomp filter active", probe["Seccomp"] == SECCOMP_FILTER_MODE,
                 f"Seccomp mode {probe['Seccomp']}")
    report.check(f"oci[{label}] seccomp blocks mkdir", probe["mkdir_shm"] == EPERM,
                 f"mkdir /dev/shm returned {probe['mkdir_shm']}")
    report.check(f"oci[{label}] read-only root", probe["write_root"] == EROFS,
                 f"write / returned {probe['write_root']}")
    report.check(f"oci[{label}] tmpfs remains writable", probe["write_shm"] == "ok",
                 f"write /dev/shm returned {probe['write_shm']}")
    report.check(f"oci[{label}] CAP_CHOWN dropped", probe["chown_shm"] == EPERM,
                 f"chown returned {probe['chown_shm']}")
    report.check(f"oci[{label}] CAP_NET_BIND_SERVICE usable", probe["bind_80"] == "ok",
                 f"bind :80 returned {probe['bind_80']}")
    lsm_label = probe["lsm_label"] or ""
    if args.apparmor_profile:
        report.check(f"oci[{label}] AppArmor profile", lsm_label.split(" (")[0] == args.apparmor_profile,
                     f"label {lsm_label!r}")
    if args.selinux_label:
        report.check(f"oci[{label}] SELinux label", lsm_label == args.selinux_label,
                     f"label {lsm_label!r}")


def run_oci_checks(report, args, layout, image_config):
    runtime = find_runtime(args.runtime)
    rootless = os.geteuid() != 0
    bundle = layout.parent / "bundle"
    bundle.mkdir()
    shutil.move(str(layout.parent / "rootfs"), bundle / "rootfs")
    spec_command = [runtime, "spec", "--bundle", str(bundle)]
    if rootless:
        spec_command.insert(2, "--rootless")
    subprocess.run(spec_command, check=True, timeout=30)
    base_spec = json.loads((bundle / "config.json").read_text())
    fragment = json.loads((layout / SECURITY_DIR / OCI_FRAGMENT).read_text())

    users = [0] if rootless else [0, NOBODY_UID]
    if rootless:
        report.skip("oci[uid 65534]", "rootless runtime maps only the invoking user; run as root to probe a non-root uid")
    for uid in users:
        spec = merge_oci_runtime_fragment(json.loads(json.dumps(base_spec)), fragment)
        process = spec["process"]
        process["terminal"] = False
        process["args"] = (image_config.get("Entrypoint") or []) + (image_config.get("Cmd") or [])
        process["env"] = (image_config.get("Env") or []) + ["PYTHONDONTWRITEBYTECODE=1"]
        process["cwd"] = image_config.get("WorkingDir") or "/"
        process["user"] = {"uid": uid, "gid": uid}
        spec["root"]["path"] = "rootfs"
        (bundle / "config.json").write_text(json.dumps(spec, indent=2))
        label = f"uid {uid}"
        result = run_runtime(runtime, bundle, args.timeout, f"pycontainer-security-{uuid.uuid4().hex[:12]}")
        probe = parse_probe(result.stdout)
        if result.returncode or probe is None:
            report.record("FAIL", f"oci[{label}] container run",
                          f"exit {result.returncode}: {(result.stderr or result.stdout).strip()[-500:]}")
            continue
        report.record("PASS", f"oci[{label}] container run", Path(runtime).name)
        check_oci_probe(report, label, probe, args)


def kubectl(kubectl_path, args, timeout, **kwargs):
    return subprocess.run([kubectl_path, *args], capture_output=True, text=True, timeout=timeout, check=False, **kwargs)


def run_kubernetes_checks(report, args, layout):
    kubectl_path = shutil.which("kubectl")
    if kubectl_path is None:
        report.skip("kubernetes", "kubectl not found")
        return
    reachable = kubectl(kubectl_path, ["auth", "can-i", "create", "pods", "-n", args.namespace], 30)
    if reachable.returncode or reachable.stdout.strip() != "yes":
        report.skip("kubernetes", f"no cluster access to create pods: {(reachable.stderr or reachable.stdout).strip()}")
        return
    security_context = json.loads((layout / SECURITY_DIR / KUBERNETES_FRAGMENT).read_text())
    pod_name = f"pycontainer-security-{uuid.uuid4().hex[:8]}"
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name, "namespace": args.namespace},
        "spec": {
            "restartPolicy": "Never",
            "containers": [{
                "name": "probe",
                "image": args.base_image,
                "command": ["python3", "-c", PROBE_SOURCE],
                "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                "securityContext": security_context,
            }],
        },
    }
    manifest = json.dumps(pod)
    dry_run = kubectl(kubectl_path, ["apply", "--dry-run=server", "--validate=strict", "-f", "-"], 60, input=manifest)
    report.check("kubernetes server dry-run (strict)", dry_run.returncode == 0,
                 (dry_run.stderr or dry_run.stdout).strip())
    if dry_run.returncode:
        return
    created = kubectl(kubectl_path, ["apply", "-f", "-"], 60, input=manifest)
    if created.returncode:
        report.record("FAIL", "kubernetes pod create", created.stderr.strip())
        return
    try:
        waited = kubectl(kubectl_path, ["wait", "-n", args.namespace, f"pod/{pod_name}",
                                        "--for=jsonpath={.status.phase}=Succeeded", f"--timeout={args.timeout}s"],
                         args.timeout + 30)
        logs = kubectl(kubectl_path, ["logs", "-n", args.namespace, pod_name], 60)
        probe = parse_probe(logs.stdout)
        if waited.returncode or probe is None:
            report.record("FAIL", "kubernetes pod run", (waited.stderr or logs.stderr or logs.stdout).strip()[-500:])
            return
        report.record("PASS", "kubernetes pod run", f"uid {probe['uid']}")
        report.check("kubernetes CapBnd", probe["CapBnd"] == EXPECTED_CAPABILITY_HEX, f"got {probe['CapBnd']}")
        if probe["uid"] == 0:
            report.check("kubernetes CapEff", probe["CapEff"] == EXPECTED_CAPABILITY_HEX, f"got {probe['CapEff']}")
        report.check("kubernetes seccomp RuntimeDefault active", probe["Seccomp"] == SECCOMP_FILTER_MODE,
                     f"Seccomp mode {probe['Seccomp']}")
        report.check("kubernetes read-only root", probe["write_root"] == EROFS, f"write / returned {probe['write_root']}")
    finally:
        kubectl(kubectl_path, ["delete", "pod", "-n", args.namespace, pod_name, "--wait=false"], 60)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-image", default="python:3.11-slim", help="registry base image (default: python:3.11-slim)")
    parser.add_argument("--runtime", choices=("runc", "crun"), help="OCI runtime to use (default: first of runc, crun)")
    parser.add_argument("--timeout", type=int, default=120, help="timeout in seconds for container runs")
    parser.add_argument("--apparmor-profile", help="AppArmor profile already loaded on this host to apply and verify")
    parser.add_argument("--selinux-label", help="SELinux process label to apply and verify")
    parser.add_argument("--kubernetes", action="store_true", help="also validate and run the Kubernetes fragment")
    parser.add_argument("--namespace", default="default", help="Kubernetes namespace for --kubernetes")
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    if not sys.platform.startswith("linux"):
        raise SystemExit("this check requires Linux; it applies controls with a local OCI runtime")
    if not hasattr(tarfile, "tar_filter"):
        raise SystemExit("this check requires a Python with tarfile extraction filters (3.11.4+)")

    report = Report()
    oci_settings = {
        "capabilities": {name: ["CAP_NET_BIND_SERVICE"] for name in
                         ("bounding", "effective", "permitted", "inheritable", "ambient")},
        "seccomp_profile": "seccomp.json",
        "read_only_root": True,
    }
    if args.apparmor_profile:
        if apparmor_enabled():
            oci_settings["apparmor_profile"] = args.apparmor_profile
        else:
            report.skip("oci AppArmor profile", "AppArmor is not enabled on this host")
            args.apparmor_profile = None
    else:
        report.skip("oci AppArmor profile", "pass --apparmor-profile with a profile loaded on this host")
    if args.selinux_label:
        if selinux_enabled():
            oci_settings["selinux_label"] = args.selinux_label
        else:
            report.skip("oci SELinux label", "SELinux is not enabled on this host")
            args.selinux_label = None
    else:
        report.skip("oci SELinux label", "pass --selinux-label to verify on an SELinux host")
    security = {
        "oci": oci_settings,
        "kubernetes": {
            "capabilities": {"drop": ["ALL"], "add": ["NET_BIND_SERVICE"]},
            "seccomp": {"type": "RuntimeDefault"},
            "read_only_root_filesystem": True,
        },
    }

    with tempfile.TemporaryDirectory(prefix="pycontainer-security-check-") as temp_dir:
        work = Path(temp_dir)
        layout = build_probe_image(args, work, host_target_platform(), security)
        image_config = unpack_image(layout, "security-check", work / "rootfs")
        run_oci_checks(report, args, layout, image_config)
        if args.kubernetes:
            run_kubernetes_checks(report, args, layout)
        else:
            report.skip("kubernetes", "pass --kubernetes to validate and run the fragment on a cluster")

    passed = sum(status == "PASS" for status, _, _ in report.results)
    skipped = sum(status == "SKIP" for status, _, _ in report.results)
    failed = sum(status == "FAIL" for status, _, _ in report.results)
    print(f"\n{passed} passed, {skipped} skipped, {failed} failed (SKIP is not evidence of enforcement)")
    if report.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
