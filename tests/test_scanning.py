import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig


BASE_CONFIG = {
    "architecture": "amd64", "os": "linux",
    "config": {"Env": [], "WorkingDir": "/"},
    "rootfs": {"type": "layers", "diff_ids": []},
}


@pytest.fixture
def scanner(tmp_path):
    script = tmp_path / "fake_grype.py"
    script.write_text(
        """import base64, datetime, json, os, pathlib, sys, time
args = sys.argv[1:]
mode = json.loads(pathlib.Path(__file__).with_name('mode.json').read_text())
with pathlib.Path(__file__).with_name('calls.jsonl').open('a') as calls:
    calls.write(json.dumps(args) + '\\n')
cfg = json.loads(pathlib.Path(args[args.index('--config') + 1]).read_text())
dbfile = pathlib.Path(cfg['db']['cache-dir']) / '6' / 'vulnerability.db'
dbfile.parent.mkdir(parents=True, exist_ok=True)
if not dbfile.exists(): dbfile.write_bytes(b'controlled fake database')
built = (datetime.datetime.now(datetime.timezone.utc) -
         datetime.timedelta(hours=mode.get('age', 1))).isoformat()
db = {'schemaVersion': '6.0.0', 'built': built, 'from': 'controlled',
      'path': str(dbfile), 'valid': not mode.get('invalid_db', False)}
# Use a stable timestamp across status and scan.
stamp = dbfile.with_suffix('.stamp')
if not stamp.exists(): stamp.write_text(built)
db['built'] = stamp.read_text()
if 'version' in args:
    print(json.dumps({'application': 'grype', 'supportedDbSchema': 6,
                      'platform': mode.get('host', 'linux/amd64'),
                      'version': mode.get('version', '0.119.0')}))
elif 'db' in args:
    if mode.get('db_error'): sys.exit(1)
    print(json.dumps(db))
else:
    if mode.get('timeout'): time.sleep(10)
    if mode.get('error'): sys.exit(1)
    if mode.get('malformed'): print('{'); sys.exit(0)
    layout = pathlib.Path(args[0].removeprefix('oci-dir:'))
    index = json.loads((layout / 'index.json').read_text())
    digest = index['manifests'][0]['digest']
    blob = layout / 'blobs' / 'sha256' / digest.split(':')[1]
    manifest = json.loads(blob.read_text())
    imagecfg = json.loads((layout / 'blobs' / 'sha256' /
                          manifest['config']['digest'].split(':')[1]).read_text())
    configblob = layout / 'blobs' / 'sha256' / manifest['config']['digest'].split(':')[1]
    doc = {'matches': [
        {'vulnerability': {'id': 'CVE-FAKE-' + str(i), 'severity': severity},
         'artifact': {'name': 'fixture', 'version': '1.0', 'type': 'python'}}
        for i, severity in enumerate(mode.get('severities', []))],
        'source': {'type': 'image', 'target': {
            'manifestDigest': digest if not mode.get('wrong_digest') else 'sha256:' + '0'*64,
            'imageID': manifest['config']['digest'],
            'manifest': base64.b64encode(blob.read_bytes()).decode(),
            'config': base64.b64encode(configblob.read_bytes()).decode(),
            'architecture': imagecfg['architecture'], 'os': imagecfg['os'],
            'layers': manifest['layers']}},
        'distro': {'name': 'debian', 'version': '12'},
        'descriptor': {'name': 'grype', 'version': '0.119.0', 'db': db,
                       'configuration': cfg}}
    if mode.get('missing_matches'): del doc['matches']
    if mode.get('bad_config'): doc['source']['target']['config'] = 'not base64!'
    if mode.get('unknown_distro'): doc['distro']['name'] = 'unknown'
    if mode.get('ignored'): doc['ignoredMatches'] = [{'reason': 'ambient ignore'}]
    if mode.get('db_changed'):
        with dbfile.open('ab') as stream: stream.write(b'changed database')
    if mode.get('mutate_source'):
        pathlib.Path(mode['mutate_source']).write_bytes(b'changed source')
    if mode.get('mutate_snapshot'): blob.write_bytes(b'changed snapshot')
    cfg['db']['max-allowed-built-age'] = int(cfg['db']['max-allowed-built-age'][:-1]) * 3600 * 1000000000
    cfg['db']['max-update-check-frequency'] = 0
    print(json.dumps(doc))
""",
        encoding="utf-8",
    )
    launcher = tmp_path / ("grype.cmd" if os.name == "nt" else "grype")
    if os.name == "nt":
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n')
    else:
        launcher.write_text(f'#!{sys.executable}\nexec(compile(open({str(script)!r}).read(), {str(script)!r}, "exec"))\n')
        launcher.chmod(0o755)
    mode = tmp_path / "mode.json"
    mode.write_text("{}")
    return launcher, mode


def make_builder(tmp_path, monkeypatch, scanner, **overrides):
    executable, _ = scanner
    context = tmp_path / "context"
    context.mkdir(exist_ok=True)
    (context / "app.py").write_text("print('hello')\n")
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda *args: ([], BASE_CONFIG),
    )
    return ImageBuilder(BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "output"),
        include_paths=["app.py"], use_cache=False, scan=True,
        scan_executable=str(executable), scan_db_dir=str(tmp_path / "db"),
        **overrides,
    ))


@pytest.mark.parametrize("severity,fails", [
    ("negligible", False), ("low", False), ("medium", False),
    ("high", True), ("critical", True), ("unknown", True),
    (None, True), ("new-severity", True),
])
def test_gate_retains_report_on_findings(tmp_path, monkeypatch, scanner, severity, fails):
    from pycontainer.scanning import VulnerabilityFoundError
    scanner[1].write_text(json.dumps({"severities": [severity]}))
    builder = make_builder(tmp_path, monkeypatch, scanner)
    if fails:
        with pytest.raises(VulnerabilityFoundError):
            builder.build()
    else:
        builder.build()
    report = json.loads((tmp_path / "output" / "vulnerabilities.json").read_text())
    assert report["pycontainer"]["image_digest"] == builder.manifest_digest
    assert report["pycontainer"]["passed"] is not fails
    assert report["pycontainer"]["database"]["digest"].startswith("sha256:")
    assert report["matches"][0]["vulnerability"]["severity"] == severity


@pytest.mark.parametrize("policy", [
    {"scan_threshold": "critical"}, {"scan_unknown": "ignore"},
    {"scan_threshold": "low"},
])
def test_policy_overrides(tmp_path, monkeypatch, scanner, policy):
    from pycontainer.scanning import VulnerabilityFoundError
    severity = "unknown" if "scan_unknown" in policy else "high"
    scanner[1].write_text(json.dumps({"severities": [severity]}))
    builder = make_builder(tmp_path, monkeypatch, scanner, **policy)
    if policy.get("scan_threshold") == "low":
        with pytest.raises(VulnerabilityFoundError):
            builder.build()
    else:
        builder.build()


@pytest.mark.parametrize("mode,category", [
    ({"version": "0.118.0"}, "scanner"),
    ({"host": "windows/amd64"}, "unsupported"),
    ({"error": True}, "scanner"), ({"malformed": True}, "scanner"),
    ({"missing_matches": True}, "scanner"),
    ({"bad_config": True}, "scanner"),
    ({"unknown_distro": True}, "unsupported"),
    ({"db_error": True}, "database"), ({"invalid_db": True}, "database"),
    ({"age": 121}, "database"), ({"age": -1}, "database"),
    ({"db_changed": True}, "database"),
    ({"wrong_digest": True}, "identity"),
    ({"mutate_snapshot": True}, "identity"),
    ({"ignored": True}, "scanner"),
])
def test_operational_errors_never_authorize_push(tmp_path, monkeypatch, scanner, mode, category):
    from pycontainer.scanning import ScanError
    scanner[1].write_text(json.dumps(mode))
    builder = make_builder(tmp_path, monkeypatch, scanner)
    transfers = []
    monkeypatch.setattr("pycontainer.builder.get_registry_auth",
                        lambda *a, **kw: transfers.append("auth"))
    with pytest.raises(ScanError) as error:
        builder.build()
    assert error.value.category == category
    with pytest.raises(ScanError):
        builder.push(show_progress=False)
    assert transfers == []


def test_sarif_and_offline(tmp_path, monkeypatch, scanner):
    from pycontainer.scanning import VulnerabilityFoundError
    scanner[1].write_text(json.dumps({"severities": ["critical"]}))
    builder = make_builder(tmp_path, monkeypatch, scanner,
                           scan_report_format="sarif", scan_offline=True)
    with pytest.raises(VulnerabilityFoundError):
        builder.build()
    report = json.loads((tmp_path / "output" / "vulnerabilities.sarif").read_text())
    assert report["version"] == "2.1.0"
    run = report["runs"][0]
    assert run["results"][0]["ruleId"] == "CVE-FAKE-0"
    assert run["properties"]["image_digest"] == builder.manifest_digest
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert not any("update" in call for call in calls)


def test_missing_tool_and_report_write_error(tmp_path, monkeypatch, scanner):
    from pycontainer.scanning import ScanError
    builder = make_builder(tmp_path, monkeypatch, scanner)
    builder.config.scan_executable = str(tmp_path / "absent")
    with pytest.raises(ScanError, match="scanner"):
        builder.build()
    builder.config.scan_executable = str(scanner[0])
    builder.config.scan_report = str(tmp_path / "output")
    with pytest.raises(ScanError) as error:
        builder.build()
    assert error.value.category == "report"


def test_default_build_never_invokes_scanner(tmp_path, monkeypatch, scanner):
    builder = make_builder(tmp_path, monkeypatch, scanner)
    builder.config.scan = False
    builder.build()
    assert not (tmp_path / "calls.jsonl").exists()


def test_changed_layer_blocks_push_before_auth(tmp_path, monkeypatch, scanner):
    from pycontainer.scanning import ScanError
    builder = make_builder(tmp_path, monkeypatch, scanner)
    builder.build()
    blob = tmp_path / "output" / "blobs" / "sha256" / builder.layers[-1].digest.split(":")[1]
    content = blob.read_bytes()
    blob.write_bytes(b"x" + content[1:])
    monkeypatch.setattr("pycontainer.builder.get_registry_auth",
                        lambda *a, **kw: pytest.fail("auth before gate"))
    with pytest.raises(ScanError) as error:
        builder.push(show_progress=False)
    assert error.value.category == "identity"


def test_new_build_changes_scanned_digest(tmp_path, monkeypatch, scanner):
    builder = make_builder(tmp_path, monkeypatch, scanner)
    builder.build()
    first = builder.manifest_digest
    (tmp_path / "context" / "app.py").write_text("print('changed')\n")
    builder.build()
    assert first != builder.manifest_digest
    report = json.loads((tmp_path / "output" / "vulnerabilities.json").read_text())
    assert report["source"]["target"]["manifestDigest"] == builder.manifest_digest


def test_push_transfers_scanned_copy_even_if_output_changes(tmp_path, monkeypatch, scanner):
    builder = make_builder(tmp_path, monkeypatch, scanner)
    builder.build()
    manifest = tmp_path / "output" / "blobs" / "sha256" / builder.manifest_digest.split(":")[1]
    scanner[1].write_text(json.dumps({"mutate_source": str(manifest)}))
    transferred = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def push_blob(self, digest, path, **kwargs):
            assert path.is_file()
            assert "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() == digest
            assert not path.is_relative_to(tmp_path / "output")
            transferred.append(digest)
            return True

        def push_manifest(self, tag, data):
            assert "sha256:" + hashlib.sha256(data).hexdigest() == builder.manifest_digest
            transferred.append(tag)

    monkeypatch.setattr("pycontainer.builder.RegistryClient", Client)
    monkeypatch.setattr("pycontainer.builder.get_registry_auth",
                        lambda *a, **kw: SimpleNamespace(bearer_token=None, username=None, password=None))
    builder.push(show_progress=False)
    assert transferred[-1] == "latest"
    assert manifest.read_bytes() == b"changed source"


@pytest.mark.parametrize("severity", ["high", "critical", "unknown"])
def test_push_rechecks_findings_and_report_writes(tmp_path, monkeypatch, scanner, severity):
    from pycontainer.scanning import ScanError, VulnerabilityFoundError
    builder = make_builder(tmp_path, monkeypatch, scanner)
    builder.build()
    monkeypatch.setattr("pycontainer.builder.get_registry_auth",
                        lambda *a, **kw: pytest.fail("auth before gate"))
    scanner[1].write_text(json.dumps({"severities": [severity]}))
    with pytest.raises(VulnerabilityFoundError):
        builder.push(show_progress=False)
    scanner[1].write_text("{}")
    builder.config.scan_report = str(tmp_path / "output")
    with pytest.raises(ScanError) as error:
        builder.push(show_progress=False)
    assert error.value.category == "report"


def test_previous_clean_report_is_not_retained_as_current_evidence(tmp_path, monkeypatch, scanner):
    from pycontainer.scanning import ScanError
    builder = make_builder(tmp_path, monkeypatch, scanner)
    builder.build()
    scanner[1].write_text(json.dumps({"error": True}))
    (tmp_path / "context" / "app.py").write_text("print('different image')\n")
    with pytest.raises(ScanError):
        builder.build()
    report = json.loads((tmp_path / "output" / "vulnerabilities.json").read_text())
    assert report["pycontainer"]["passed"] is False
    assert report["pycontainer"]["scan_status"] == "incomplete"
    assert report["pycontainer"]["image_digest"] == builder.manifest_digest


def test_database_lock_is_operational_failure(tmp_path, monkeypatch, scanner):
    from pycontainer.fs_utils import output_directory_lock
    from pycontainer.scanning import ScanError
    builder = make_builder(tmp_path, monkeypatch, scanner)
    with output_directory_lock(tmp_path / "db"):
        with pytest.raises(ScanError) as error:
            builder.build()
    assert error.value.category == "database"


@pytest.mark.parametrize("format", ["json", "sarif"])
def test_report_cannot_overwrite_image_metadata(tmp_path, monkeypatch, scanner, format):
    from pycontainer.scanning import ScanError
    builder = make_builder(tmp_path, monkeypatch, scanner,
                           scan_report_format=format,
                           scan_report=str(tmp_path / "output" / "index.json"))
    with pytest.raises(ScanError) as error:
        builder.build()
    assert error.value.category == "report"
    assert json.loads((tmp_path / "output" / "index.json").read_text())["schemaVersion"] == 2


def test_process_timeout_and_output_limit(tmp_path, monkeypatch, scanner):
    from pycontainer import scanning
    # Exercise a real Python executable, not a Windows batch parent process.
    cfg = tmp_path / "unused.json"
    cfg.write_text("{}")
    monkeypatch.setattr(scanning, "SCAN_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(scanning.ScanError, match="timeout"):
        scanning._run(sys.executable, ["-c", "import time; time.sleep(10)"],
                      cfg, os.environ.copy(), "scanner")
    monkeypatch.setattr(scanning, "MAX_OUTPUT_BYTES", 10)
    monkeypatch.setattr(scanning, "SCAN_TIMEOUT_SECONDS", 10)
    with pytest.raises(scanning.ScanError, match="size limit"):
        scanning._run(sys.executable, ["-c", "print('x' * 100)"],
                      cfg, os.environ.copy(), "scanner")


@pytest.mark.parametrize("policy", [
    {"scan_threshold": "unknown"}, {"scan_unknown": "allow"},
    {"scan_report_format": "text"}, {"scan_db_max_age_hours": 0},
    {"scan_db_max_age_hours": True}, {"scan_executable": ""},
])
def test_invalid_scan_policies_rejected(policy):
    with pytest.raises(ValueError):
        BuildConfig(**policy)


def test_unsupported_target_fails_before_scanner(tmp_path, monkeypatch, scanner):
    from pycontainer.scanning import ScanError
    builder = make_builder(tmp_path, monkeypatch, scanner, platform="linux/s390x")
    with pytest.raises(ScanError) as error:
        builder.build()
    assert error.value.category == "unsupported"
    assert not (tmp_path / "calls.jsonl").exists()
