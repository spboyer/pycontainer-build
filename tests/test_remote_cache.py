import hashlib
import json
from pathlib import Path

import pytest

from pycontainer.blob_store import BlobIntegrityError
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file
from pycontainer.registry_client import RegistryRequestError
from pycontainer.remote_cache import (
    LocalDirectoryBackend,
    RegistryBackend,
    RemoteCache,
    RemoteCacheError,
    parse_cache_spec,
)


class FakeRegistryClient:
    def __init__(self):
        self.blobs = {}
        self.manifests = {}

    def blob_exists(self, digest):
        return digest in self.blobs

    def push_blob(self, digest, path, check_exists=True):
        data = Path(path).read_bytes()
        assert f"sha256:{hashlib.sha256(data).hexdigest()}" == digest
        self.blobs[digest] = data
        return True

    def push_manifest(self, reference, data):
        self.manifests[reference] = data
        return True

    def pull_manifest(self, reference, **_):
        if reference not in self.manifests:
            raise RegistryRequestError("HTTP 404")
        data = self.manifests[reference]
        return json.loads(data), f"sha256:{hashlib.sha256(data).hexdigest()}"

    def pull_blob(self, digest, dest_path, expected_size):
        if digest not in self.blobs:
            raise RegistryRequestError("HTTP 404")
        data = self.blobs[digest]
        if hashlib.sha256(data).hexdigest() != digest.split(":")[1]:
            raise BlobIntegrityError("corrupt")
        Path(dest_path).write_bytes(data)
        return True


def make_context(tmp_path, content="print('hi')\n"):
    context = tmp_path / "ctx"
    context.mkdir(exist_ok=True)
    (context / "app.py").write_text(content)
    return context


def make_builder(tmp_path, name, context, **overrides):
    config = BuildConfig(
        context_dir=str(context),
        output_dir=str(tmp_path / name / "out"),
        cache_dir=str(tmp_path / name / "cache"),
        include_paths=["app.py"],
        **overrides,
    )
    return ImageBuilder(config)


def app_layer(builder, tmp_path, name):
    layers = tmp_path / name / "layers"
    layers.mkdir(parents=True, exist_ok=True)
    return builder._create_app_layer(layers, ["app.py"])


def test_local_backend_round_trip_hits_in_fresh_cache(tmp_path):
    context = make_context(tmp_path)
    shared = tmp_path / "shared"
    producer = make_builder(tmp_path, "a", context, cache_to=[f"type=local,dest={shared}"])
    layer = app_layer(producer, tmp_path, "a")
    producer.remote_cache.export()

    consumer = make_builder(tmp_path, "b", context, cache_from=[f"type=local,src={shared}"])
    (tmp_path / "b" / "layers").mkdir(parents=True)
    imported = consumer._import_remote_layer(
        [(context / "app.py", Path("app.py"))], tmp_path / "b" / "layers"
    )
    assert imported is not None
    assert imported.digest == layer.digest
    assert Path(imported.tar_path).is_file()


def test_registry_backend_round_trip_with_fake_registry(tmp_path):
    context = make_context(tmp_path)
    client = FakeRegistryClient()
    cache = RemoteCache([], [(RegistryBackend("reg.example/cache:main", client), "")])
    producer = make_builder(tmp_path, "a", context)
    producer.remote_cache = cache
    layer = app_layer(producer, tmp_path, "a")
    cache.export()

    assert layer.digest in client.blobs
    manifest = json.loads(client.manifests["main"])
    assert [entry["digest"] for entry in manifest["layers"]] == [layer.digest]

    consumer = make_builder(tmp_path, "b", context)
    consumer.remote_cache = RemoteCache([(RegistryBackend("reg.example/cache:main", client), "")], [])
    (tmp_path / "b" / "layers").mkdir(parents=True)
    imported = consumer._import_remote_layer(
        [(context / "app.py", Path("app.py"))], tmp_path / "b" / "layers"
    )
    assert imported.digest == layer.digest
    # imported layer also populated the consumer's local cache
    assert consumer.cache.get_layer([(context / "app.py", Path("app.py"))]) is not None


def test_corrupt_remote_blob_is_discarded_and_layer_rebuilt(tmp_path):
    context = make_context(tmp_path)
    client = FakeRegistryClient()
    cache = RemoteCache([], [(RegistryBackend("reg.example/cache", client), "")])
    producer = make_builder(tmp_path, "a", context)
    producer.remote_cache = cache
    layer = app_layer(producer, tmp_path, "a")
    cache.export()
    client.blobs[layer.digest] = b"tampered"

    consumer = make_builder(tmp_path, "b", context)
    consumer.remote_cache = RemoteCache([(RegistryBackend("reg.example/cache", client), "")], [])
    rebuilt = app_layer(consumer, tmp_path, "b")
    assert rebuilt.digest == layer.digest
    assert hashlib.sha256(Path(rebuilt.tar_path).read_bytes()).hexdigest() == layer.digest.split(":")[1]
    assert not list((tmp_path / "b" / "layers").glob(".*.tmp"))


def test_corrupt_local_directory_blob_is_not_trusted(tmp_path):
    context = make_context(tmp_path)
    shared = tmp_path / "shared"
    producer = make_builder(tmp_path, "a", context, cache_to=[f"type=local,dest={shared}"])
    layer = app_layer(producer, tmp_path, "a")
    producer.remote_cache.export()
    blob = shared / "blobs" / "sha256" / layer.digest.split(":")[1]
    blob.write_bytes(b"x" * blob.stat().st_size)

    consumer = make_builder(tmp_path, "b", context, cache_from=[f"type=local,src={shared}"])
    (tmp_path / "b" / "layers").mkdir(parents=True)
    assert consumer._import_remote_layer(
        [(context / "app.py", Path("app.py"))], tmp_path / "b" / "layers"
    ) is None
    # a later export repairs the corrupt blob
    repair = make_builder(tmp_path, "c", context, cache_to=[f"type=local,dest={shared}"])
    app_layer(repair, tmp_path, "c")
    repair.remote_cache.export()
    assert LocalDirectoryBackend(shared).has_blob(layer.digest)


def test_missing_remote_blob_and_index_are_misses(tmp_path):
    context = make_context(tmp_path)
    client = FakeRegistryClient()
    consumer = make_builder(tmp_path, "b", context)
    consumer.remote_cache = RemoteCache([(RegistryBackend("reg.example/cache", client), "")], [])
    (tmp_path / "b" / "layers").mkdir(parents=True)
    files = [(context / "app.py", Path("app.py"))]
    assert consumer._import_remote_layer(files, tmp_path / "b" / "layers") is None

    cache = RemoteCache([], [(RegistryBackend("reg.example/cache", client), "")])
    producer = make_builder(tmp_path, "a", context)
    producer.remote_cache = cache
    layer = app_layer(producer, tmp_path, "a")
    cache.export()
    del client.blobs[layer.digest]
    consumer.remote_cache = RemoteCache([(RegistryBackend("reg.example/cache", client), "")], [])
    assert consumer._import_remote_layer(files, tmp_path / "b" / "layers") is None


def test_scope_isolates_entries(tmp_path):
    context = make_context(tmp_path)
    shared = tmp_path / "shared"
    producer = make_builder(tmp_path, "a", context, cache_to=[f"type=local,dest={shared},scope=svc-a"])
    app_layer(producer, tmp_path, "a")
    producer.remote_cache.export()

    files = [(context / "app.py", Path("app.py"))]
    (tmp_path / "b" / "layers").mkdir(parents=True)
    other = make_builder(tmp_path, "b", context, cache_from=[f"type=local,src={shared},scope=svc-b"])
    assert other._import_remote_layer(files, tmp_path / "b" / "layers") is None
    same = make_builder(tmp_path, "c", context, cache_from=[f"type=local,src={shared},scope=svc-a"])
    (tmp_path / "c" / "layers").mkdir(parents=True)
    assert same._import_remote_layer(files, tmp_path / "c" / "layers") is not None


def test_export_failure_does_not_fail_build(tmp_path):
    class Broken(LocalDirectoryBackend):
        def write_index(self, index):
            raise OSError("disk full")

    context = make_context(tmp_path)
    builder = make_builder(tmp_path, "a", context)
    builder.remote_cache = RemoteCache([], [(Broken(tmp_path / "s"), "")])
    app_layer(builder, tmp_path, "a")
    builder.remote_cache.export()


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "type=s3,bucket=x",
        "type=registry",
        "type=registry,ref=reg.example/cache,bogus=1",
        "type=local,dest=/x",
        "type=registry,ref=reg.example/cache,scope=bad/scope",
        "ref",
    ],
)
def test_invalid_cache_from_specs_are_rejected(spec):
    with pytest.raises(RemoteCacheError):
        parse_cache_spec(spec, export=False)


def test_local_cache_to_requires_dest_and_registry_spec_parses():
    with pytest.raises(RemoteCacheError):
        parse_cache_spec("type=local,src=/x", export=True)
    backend, scope = parse_cache_spec(
        "type=registry,ref=ghcr.io/me/cache:ci,scope=main", export=True
    )
    assert (backend.registry, backend.repository, backend.tag, scope) == (
        "ghcr.io",
        "me/cache",
        "ci",
        "main",
    )


def test_cache_options_load_from_config_file(tmp_path):
    path = tmp_path / "pycontainer.toml"
    path.write_text(
        '[build]\ncache-from = ["type=registry,ref=reg.example/cache"]\n'
        'cache-to = ["type=local,dest=out"]\n'
    )
    config = config_from_file(path, {})
    assert config.cache_from == ["type=registry,ref=reg.example/cache"]
    assert config.cache_to == ["type=local,dest=out"]
