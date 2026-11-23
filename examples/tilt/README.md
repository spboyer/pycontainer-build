# Tilt custom_build example

A dependency-free HTTP service built by pycontainer-build as a
[Tilt `custom_build`](https://docs.tilt.dev/custom_build.html) with
`skips_local_docker=True`. pycontainer pushes the image itself; Tilt deploys it.

Requirements: Tilt, `kubectl` with a cluster context, pycontainer-build
installed in the active Python environment, and a registry the cluster can
pull from (see [registry transport](../../docs/skaffold-tilt.md#what-is-supported)
and [credentials](../../docs/skaffold-tilt.md#credentials)).

```bash
cd examples/tilt
export PYCONTAINER_TILT_REGISTRY=example.azurecr.io/dev
tilt up            # pushes exactly $EXPECTED_REF

# outputs_image_ref_to variant: pushes <registry>/pycontainer-demo:tilt-<digest>
PYCONTAINER_TILT_MODE=output-ref tilt up
```

`custom_build(tag=...)` is not supported, and the registry must be explicit:
without `default_registry()` (or a cluster-advertised registry) Tilt passes a
registry-less `EXPECTED_REF`, which pycontainer rejects. The example disables
Tilt's background Docker pruner to preserve the local cache.

See [docs/skaffold-tilt.md](../../docs/skaffold-tilt.md) for the full contract.
