# Skaffold custom builder example

A dependency-free HTTP service built by pycontainer-build as a
[Skaffold custom builder](https://skaffold.dev/docs/builders/builder-types/custom/)
and pushed straight to a registry, with no Docker daemon involved.

Requirements: Skaffold v2, `kubectl` with a cluster context, pycontainer-build
installed in the active Python environment, and an HTTPS registry the cluster
can pull from (see [credentials](../../docs/skaffold-tilt.md#credentials)).

```bash
cd examples/skaffold
skaffold run --default-repo example.azurecr.io/dev
# or iterate with: skaffold dev --default-repo example.azurecr.io/dev
kubectl port-forward deployment/pycontainer-demo 8000:8000
```

`skaffold.yaml` sets `build.local.push: true`. Without it Skaffold asks for
`PUSH_IMAGE=false` on local clusters, which pycontainer rejects because it
cannot load an image into a Docker daemon. Requesting more than one platform
is rejected until [#29](https://github.com/spboyer/pycontainer-build/issues/29)
lands.

See [docs/skaffold-tilt.md](../../docs/skaffold-tilt.md) for the full contract.
