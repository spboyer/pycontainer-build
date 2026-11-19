var builder = DistributedApplication.CreateBuilder(args);

// Any OCI registry pycontainer can authenticate to, e.g. endpoint "ghcr.io" and repository "<owner>".
var registry = builder.AddContainerRegistry(
    "registry",
    builder.AddParameter("registry-endpoint"),
    builder.AddParameter("registry-repository"));

builder.AddUvicornApp("api", "../api", "main:app")
    .WithUv()
    .WithExternalHttpEndpoints()
    .WithContainerRegistry(registry)
    .WithPyContainerBuild();

builder.Build().Run();
