"""Fail-closed, opt-in Grype adapter. No runtime dependency or daemon required."""
import base64
import binascii
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .blob_store import HASH_CHUNK_SIZE, blob_matches, digest_filename, hash_file
from .config import SCAN_SEVERITIES
from .fs_utils import output_directory_lock


GRYPE_VERSION = "0.119.0"
SCAN_TIMEOUT_SECONDS = 300
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_LAYERS = 1000
COVERAGE = (
    "Grype squashed image cataloging: base OS package databases and installed "
    "language package metadata, including staged Python packages. Not source "
    "analysis, malware detection, runtime validation, or proof of no vulnerabilities. "
    "Uncataloged packages and scanner/database false negatives remain possible."
)


class ScanError(RuntimeError):
    def __init__(self, category, message):
        self.category = category
        super().__init__(f"Vulnerability scan {category} error: {message}")


class VulnerabilityFoundError(ScanError):
    def __init__(self, count, report):
        super().__init__("findings", f"{count} finding(s) fail policy; report: {report}")


def _read_json(path, category="identity"):
    try:
        if path.stat().st_size > MAX_METADATA_BYTES:
            raise ScanError(category, "metadata exceeds size limit")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    except (OSError, UnicodeError, ValueError) as error:
        raise ScanError(category, f"invalid metadata: {error}") from error


def _descriptor(value):
    try:
        digest_filename(value["digest"])
        if type(value["size"]) is not int or value["size"] < 0:
            raise ValueError("invalid size")
        if not isinstance(value["mediaType"], str):
            raise ValueError("invalid media type")
        return value
    except (KeyError, TypeError, ValueError) as error:
        raise ScanError("identity", "invalid OCI descriptor") from error


def _blob(root, descriptor):
    return root / "blobs" / "sha256" / digest_filename(descriptor["digest"])


def verify_snapshot(root, descriptor):
    """Validate every byte that will be scanned or transferred."""
    try:
        if not blob_matches(_blob(root, descriptor), descriptor["digest"], descriptor["size"]):
            raise ScanError("identity", "manifest digest/size mismatch")
        manifest = _read_json(_blob(root, descriptor))
        config = _descriptor(manifest["config"])
        layers = manifest["layers"]
        if not isinstance(layers, list) or len(layers) > MAX_LAYERS:
            raise ScanError("identity", "invalid layer list")
        for item in [config, *layers]:
            item = _descriptor(item)
            if not blob_matches(_blob(root, item), item["digest"], item["size"]):
                raise ScanError("identity", f"blob digest/size mismatch: {item['digest']}")
        return manifest
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ScanError("identity", f"invalid image: {error}") from error


@contextmanager
def selected_image_snapshot(output, digest, platform):
    """Copy only the selected image; never let a scanner choose another tag."""
    try:
        index = _read_json(output / "index.json")
        manifests = index.get("manifests")
        if not isinstance(manifests, list):
            raise ScanError("identity", "missing index manifests")
        selected = [item for item in manifests if isinstance(item, dict) and item.get("digest") == digest]
        if len(selected) != 1:
            raise ScanError("identity", "selected manifest is missing or ambiguous")
        descriptor = _descriptor(selected[0])
        if descriptor["mediaType"] != "application/vnd.oci.image.manifest.v1+json":
            raise ScanError("unsupported", "only OCI image manifests are supported")
        manifest = verify_snapshot(output, descriptor)
        image_config = _read_json(_blob(output, manifest["config"]))
        parts = platform.split("/")
        if platform not in ("linux/amd64", "linux/arm64"):
            raise ScanError("unsupported", f"unsupported scan target: {platform}")
        if image_config.get("os") != parts[0] or image_config.get("architecture") != parts[1]:
            raise ScanError("identity", "image platform does not match requested target")
        with tempfile.TemporaryDirectory(prefix="pycontainer-scan-") as name:
            root = Path(name)
            blobs = root / "blobs" / "sha256"
            blobs.mkdir(parents=True)
            for item in [descriptor, manifest["config"], *manifest["layers"]]:
                source = _blob(output, item)
                destination = _blob(root, item)
                if destination.exists():
                    continue
                # Copies, not hardlinks: output mutations cannot alter the scan/push source.
                with source.open("rb") as src, destination.open("wb") as dst:
                    shutil.copyfileobj(src, dst, HASH_CHUNK_SIZE)
            verify_snapshot(root, descriptor)
            (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}', encoding="utf-8")
            (root / "index.json").write_text(json.dumps({
                "schemaVersion": 2, "manifests": [descriptor],
            }), encoding="utf-8")
            yield root, descriptor
    except OSError as error:
        raise ScanError("identity", f"cannot stage image snapshot: {error}") from error


def _run(executable, args, config_file, environment, category):
    """Drain bounded pipes into files; excess bytes are never persisted."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                [executable, *args, "--config", str(config_file)],
                cwd=config_file.parent, env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise ScanError("scanner", f"cannot execute Grype: {error}") from error
        exceeded = threading.Event()
        read_errors = []

        def drain(pipe, destination):
            size = 0
            try:
                while chunk := pipe.read1(64 * 1024):
                    size += len(chunk)
                    if size > MAX_OUTPUT_BYTES:
                        exceeded.set()
                        return
                    destination.write(chunk)
            except OSError as error:
                read_errors.append(error)
            finally:
                pipe.close()

        readers = [
            threading.Thread(target=drain, args=(process.stdout, stdout)),
            threading.Thread(target=drain, args=(process.stderr, stderr)),
        ]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + SCAN_TIMEOUT_SECONDS
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise ScanError(category, "process timeout")
                if exceeded.is_set():
                    raise ScanError(category, "process output exceeds size limit")
                time.sleep(0.05)
            for reader in readers:
                reader.join()
            if exceeded.is_set():
                raise ScanError(category, "process output exceeds size limit")
            if read_errors:
                raise ScanError(category, f"cannot read process output: {read_errors[0]}")
            if process.returncode != 0:
                stderr.seek(0)
                detail = stderr.read(4096).decode("utf-8", errors="replace").strip()
                raise ScanError(category, f"Grype exited with status {process.returncode}: {detail or 'no diagnostic output'}")
            stdout.seek(0)
            return stdout.read()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            for reader in readers:
                reader.join()


def _object(raw, category):
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value
    except (ValueError, UnicodeError) as error:
        raise ScanError(category, "missing or malformed Grype JSON output") from error


def _database(status, db_dir, max_age):
    if status.get("valid") is not True or status.get("error"):
        raise ScanError("database", "database missing or invalid")
    try:
        built = datetime.fromisoformat(status["built"].replace("Z", "+00:00"))
        if built.tzinfo is None:
            raise ValueError("database timestamp has no timezone")
        age = (datetime.now(timezone.utc) - built).total_seconds()
        if age < -300 or age > max_age * 3600:
            raise ScanError("database", "database is stale or has a future build timestamp")
        if not isinstance(status["schemaVersion"], str) or not status["schemaVersion"].startswith(("6.", "v6.")):
            raise ValueError("unsupported database schema")
        path = Path(status["path"])
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(db_dir):
            raise ValueError("database path is not a regular file in the configured cache")
        digest, size = hash_file(path)
        return {"digest": digest, "size": size, "built": status["built"],
                "schemaVersion": status["schemaVersion"], "from": status.get("from", "")}
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as error:
        raise ScanError("database", f"unverifiable database: {error}") from error


def _layers_match(reported, layers, diff_ids):
    """Grype 0.119.0 reports uncompressed diffIDs with cataloged sizes, not blob descriptors."""
    if not (isinstance(reported, list) and isinstance(diff_ids, list)
            and len(reported) == len(layers) == len(diff_ids)):
        return False
    return all(
        isinstance(item, dict) and item.get("digest") == diff_id
        and item.get("mediaType") == layer["mediaType"]
        and isinstance(item.get("size"), int) and item["size"] >= 0
        for item, layer, diff_id in zip(reported, layers, diff_ids)
    )


def _validate_document(doc, root, descriptor, platform, status, scanner_config):
    manifest = verify_snapshot(root, descriptor)
    try:
        source = doc["source"]
        target = source["target"]
        raw_manifest = base64.b64decode(target["manifest"], validate=True)
        raw_config = base64.b64decode(target["config"], validate=True)
        image_config = json.loads(raw_config)
        if (source["type"] != "image"
                or target["manifestDigest"] != descriptor["digest"]
                or target["imageID"] != manifest["config"]["digest"]
                or "sha256:" + hashlib.sha256(raw_manifest).hexdigest() != descriptor["digest"]
                or "sha256:" + hashlib.sha256(raw_config).hexdigest() != manifest["config"]["digest"]
                or f"{image_config['os']}/{image_config['architecture']}" != platform
                or not _layers_match(target["layers"], manifest["layers"],
                                     image_config["rootfs"]["diff_ids"])):
            raise ScanError("identity", "scanner did not attest the selected image")
        info = doc["descriptor"]
        if info["name"] != "grype" or info["version"] != GRYPE_VERSION:
            raise ScanError("scanner", "report scanner version mismatch")
        report_db = info["db"]["status"]
        if (not isinstance(report_db, dict) or report_db.get("valid") is not True or report_db.get("error")
                or any(report_db.get(key) != status.get(key)
                       for key in ("schemaVersion", "built", "from", "path"))):
            raise ScanError("database", "report database does not match checked database")
        configuration = info["configuration"]
        for key in ("ignore", "exclude", "only-fixed", "only-notfixed", "vex-documents", "vex-add"):
            if key not in configuration or configuration[key]:
                raise ScanError("scanner", f"report contains unexpected filtering: {key}")
        if configuration.get("search", {}).get("scope") != "squashed":
            raise ScanError("scanner", "report has unverified layer scope")
        for key, value in scanner_config["db"].items():
            if key == "max-allowed-built-age":
                value = int(value[:-1]) * 3600 * 1000000000
            elif key == "max-update-check-frequency":
                value = 0
            if configuration.get("db", {}).get(key) != value:
                raise ScanError("database", f"report database policy mismatch: {key}")
        if doc.get("ignoredMatches"):
            raise ScanError("scanner", "report contains ignored findings")
        distro = doc["distro"]
        if not distro.get("name") or distro["name"] == "unknown" or not distro.get("version"):
            raise ScanError("unsupported", "OS distribution coverage is unknown")
        if doc.get("alertsByPackage"):
            raise ScanError("unsupported", "scanner reports package/distro coverage alerts")
        matches = doc["matches"]
        if not isinstance(matches, list):
            raise ValueError("matches must be an array")
        for match in matches:
            vulnerability, artifact = match["vulnerability"], match["artifact"]
            for value in (vulnerability["id"], artifact["name"], artifact["version"], artifact["type"]):
                if not isinstance(value, str) or not value:
                    raise ValueError("finding identity is missing")
            severity = vulnerability.get("severity")
            if severity is not None and not isinstance(severity, str):
                raise ValueError("invalid severity")
        return matches
    except (KeyError, TypeError, ValueError, AttributeError, binascii.Error) as error:
        raise ScanError("scanner", f"incomplete or invalid scan report: {error}") from error


def _sarif(matches, metadata):
    rules, results = {}, []
    for match in matches:
        vulnerability, artifact = match["vulnerability"], match["artifact"]
        rule_id = vulnerability["id"]
        rules[rule_id] = {"id": rule_id, "shortDescription": {"text": rule_id}}
        severity = (vulnerability.get("severity") or "unknown").lower()
        results.append({
            "ruleId": rule_id,
            "level": "error" if severity not in ("negligible", "low", "medium") else "warning",
            "message": {"text": f"{artifact['name']} {artifact['version']}: {rule_id} ({severity})"},
            "properties": {"severity": severity, "package": {
                key: artifact[key] for key in ("name", "version", "type")
            }},
        })
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": "pycontainer-grype", "version": GRYPE_VERSION,
                                     "rules": list(rules.values())}},
                  "results": results, "properties": metadata,
                  "invocations": [{"executionSuccessful": metadata["scan_status"] == "complete"}]}],
    }


def _write_report(path, document):
    staged = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        staged = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
    except (OSError, ValueError) as error:
        raise ScanError("report", f"cannot write report {path}: {error}") from error
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


@contextmanager
def _database_lock(db_dir):
    lock = output_directory_lock(db_dir)
    try:
        lock.__enter__()
    except (OSError, RuntimeError) as error:
        raise ScanError("database", f"cannot acquire database lock: {error}") from error
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def _prepare_report(config, digest):
    executable = shutil.which(config.scan_executable)
    db_dir = Path(config.scan_db_dir or Path.home() / ".pycontainer" / "grype-db").expanduser().resolve()
    output = Path(config.output_dir).resolve()
    report = Path(config.scan_report or output / f"vulnerabilities.{config.scan_report_format}").resolve()
    if (report in (output / "index.json", output / "oci-layout")
            or any(report.is_relative_to(output / name) for name in ("blobs", "refs"))
            or report.is_relative_to(db_dir)
            or (executable and report == Path(executable).resolve())):
        raise ScanError("report", "report would overwrite image, scanner or database inputs")
    pending = {
        "image_digest": digest, "platform": config.platform,
        "scan_status": "incomplete", "passed": False,
        "attempted_at": datetime.now(timezone.utc).isoformat(),
        "coverage_limits": COVERAGE,
    }
    _write_report(report, {"pycontainer": pending} if config.scan_report_format == "json"
                  else _sarif([], pending))
    return report


@contextmanager
def scanned_image(output, digest, config):
    """Invalidate stale evidence before staging, then yield only gated bytes."""
    report = _prepare_report(config, digest)
    with selected_image_snapshot(output, digest, config.platform) as (root, descriptor):
        scan_image(root, descriptor, config, report=report)
        yield root, descriptor


def scan_image(root, descriptor, config, *, report=None):
    """Run the gate; successful return is the only authorization to transfer."""
    if report is None:
        report = _prepare_report(config, descriptor["digest"])
    executable = shutil.which(config.scan_executable)
    if not executable:
        raise ScanError("scanner", f"Grype executable not found: {config.scan_executable}")
    db_dir = Path(config.scan_db_dir or Path.home() / ".pycontainer" / "grype-db").expanduser().resolve()
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("GRYPE_", "SYFT_"))}
    scanner_config = {
        "check-for-app-update": False, "only-fixed": False, "only-notfixed": False,
        "ignore": [], "exclude": [], "vex-documents": [], "vex-add": [],
        # Otherwise Grype 0.119.0 injects default kernel-header ignore rules.
        "match-upstream-kernel-headers": True,
        "search": {"scope": "squashed"},
        "external-sources": {"enable": False},
        "db": {"cache-dir": str(db_dir), "auto-update": False,
               "validate-by-hash-on-start": True, "validate-age": True,
               "max-allowed-built-age": f"{config.scan_db_max_age_hours}h",
               "require-update-check": not config.scan_offline,
               "max-update-check-frequency": "0s"},
    }
    # Grype accepts JSON as YAML. An explicit file and clean env avoid ambient ignores.
    with tempfile.TemporaryDirectory(prefix="pycontainer-grype-") as name:
        config_file = Path(name) / "config.json"
        config_file.write_text(json.dumps(scanner_config), encoding="utf-8")
        version = _object(_run(executable, ["version", "-o", "json"], config_file, environment, "scanner"), "scanner")
        if version.get("version") != GRYPE_VERSION:
            raise ScanError("scanner", f"requires Grype {GRYPE_VERSION}; got {version.get('version')!r}")
        if (version.get("application") != "grype"
                or version.get("supportedDbSchema") != 6
                or not isinstance(version.get("platform"), str)):
            raise ScanError("scanner", "incomplete scanner version metadata")
        if version["platform"].startswith("windows/"):
            raise ScanError(
                "unsupported",
                "Grype 0.119.0 native Windows oci-dir layer caching uses invalid "
                "sha256: filenames; run the builder and scanner together on Linux/WSL "
                "or another independently validated host",
            )
        if version["platform"] not in ("linux/amd64", "linux/arm64", "darwin/amd64", "darwin/arm64"):
            raise ScanError("unsupported", f"unsupported scanner host: {version['platform']}")
        try:
            with _database_lock(db_dir):
                if not config.scan_offline:
                    _run(executable, ["db", "update"], config_file, environment, "database")
                status = _object(_run(executable, ["db", "status", "-o", "json"],
                                      config_file, environment, "database"), "database")
                database = _database(status, db_dir, config.scan_db_max_age_hours)
                raw = _run(executable, [f"oci-dir:{root}", "--platform", config.platform,
                                       "--scope", "squashed", "-o", "json"],
                           config_file, environment, "scanner")
                doc = _object(raw, "scanner")
                if _database(status, db_dir, config.scan_db_max_age_hours) != database:
                    raise ScanError("database", "database changed during scan")
                matches = _validate_document(doc, root, descriptor, config.platform, status, scanner_config)
        except OSError as error:
            raise ScanError("database", f"database access failed: {error}") from error
    threshold = SCAN_SEVERITIES.index(config.scan_threshold)
    failing = 0
    for match in matches:
        severity = (match["vulnerability"].get("severity") or "unknown").lower()
        if severity not in SCAN_SEVERITIES:
            failing += config.scan_unknown == "fail"
        else:
            failing += SCAN_SEVERITIES.index(severity) >= threshold
    metadata = {
        "image_digest": descriptor["digest"], "platform": config.platform,
        "scanner": {"name": "grype", "version": GRYPE_VERSION, "compatibility": "experimental"},
        "database": database, "scanned_at": datetime.now(timezone.utc).isoformat(),
        "policy": {"threshold": config.scan_threshold, "unknown": config.scan_unknown,
                   "offline": config.scan_offline, "max_db_age_hours": config.scan_db_max_age_hours},
        "coverage_limits": COVERAGE, "passed": not failing,
        "scan_status": "complete",
    }
    doc["pycontainer"] = metadata
    _write_report(report, doc if config.scan_report_format == "json" else _sarif(matches, metadata))
    if failing:
        raise VulnerabilityFoundError(failing, report)
    return report
