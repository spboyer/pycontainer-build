using Aspire.Hosting.ApplicationModel;
using Aspire.Hosting.Pipelines;
using Aspire.Hosting.PyContainer;
using Aspire.Hosting.Python;

namespace Aspire.Hosting;

/// <summary>
/// Extensions that publish Aspire Python resources with pycontainer-build instead of a Dockerfile build.
/// </summary>
public static class PyContainerResourceBuilderExtensions
{
    /// <summary>
    /// Builds and pushes this Python app's image with pycontainer-build during <c>aspire publish</c>,
    /// <c>aspire deploy</c> and <c>aspire do</c>, so no Docker or Podman daemon is needed.
    /// </summary>
    /// <remarks>
    /// Run mode is unchanged. In publish mode the resource's <c>build-&lt;name&gt;</c> and <c>push-&lt;name&gt;</c>
    /// pipeline steps keep their names, tags and dependencies, but run pycontainer instead of the container runtime.
    /// The entrypoint, dependencies and registry target are inferred from the existing Aspire model.
    /// </remarks>
    /// <example>
    /// <code>
    /// builder.AddUvicornApp("api", "../api", "main:app")
    ///     .WithUv()
    ///     .WithPyContainerBuild();
    /// </code>
    /// </example>
    public static IResourceBuilder<T> WithPyContainerBuild<T>(
        this IResourceBuilder<T> builder,
        Action<PyContainerBuildOptions>? configure = null) where T : PythonAppResource
    {
        ArgumentNullException.ThrowIfNull(builder);

        if (!builder.ApplicationBuilder.ExecutionContext.IsPublishMode)
        {
            return builder;
        }

        var options = new PyContainerBuildOptions();
        configure?.Invoke(options);

        var alreadyConfigured = builder.Resource.TryGetLastAnnotation<PyContainerBuildAnnotation>(out _);
        builder.WithAnnotation(new PyContainerBuildAnnotation(options), ResourceAnnotationMutationBehavior.Replace);

        if (!alreadyConfigured)
        {
            var resource = builder.Resource;
            var appHostDirectory = builder.ApplicationBuilder.AppHostDirectory;

            // Swap steps as late as possible so later PublishAsDockerFile/WithDockerfile calls, which replace
            // the resource's pipeline step annotation, cannot silently restore the container-runtime build.
            builder.ApplicationBuilder.Eventing.Subscribe<BeforeStartEvent>((_, _) =>
            {
                ReplaceImageSteps(resource, appHostDirectory);
                return Task.CompletedTask;
            });
        }

        return builder;
    }

    internal static void ReplaceImageSteps(PythonAppResource resource, string appHostDirectory)
    {
        var originals = resource.Annotations.OfType<PipelineStepAnnotation>().ToList();
        foreach (var annotation in originals)
        {
            resource.Annotations.Remove(annotation);
        }

        var buildStepName = $"build-{resource.Name}";
        var pushStepName = $"push-{resource.Name}";

        resource.Annotations.Add(new PipelineStepAnnotation(async factoryContext =>
        {
            var steps = new List<PipelineStep>();
            foreach (var annotation in originals)
            {
                steps.AddRange(await annotation.CreateStepsAsync(factoryContext).ConfigureAwait(false));
            }

            var target = factoryContext.Resource;
            var foundBuild = false;
            for (var i = 0; i < steps.Count; i++)
            {
                if (steps[i].Name == buildStepName)
                {
                    foundBuild = true;
                    steps[i] = Clone(steps[i], ctx => PyContainerImageBuilder.BuildAsync(resource, target, appHostDirectory, push: false, ctx));
                }
                else if (steps[i].Name == pushStepName)
                {
                    steps[i] = Clone(steps[i], ctx => PyContainerImageBuilder.BuildAsync(resource, target, appHostDirectory, push: true, ctx));
                }
            }

            if (!foundBuild && target.RequiresImageBuild())
            {
                throw new InvalidOperationException(
                    $"WithPyContainerBuild could not find the '{buildStepName}' pipeline step for '{resource.Name}'. " +
                    "This Aspire version may have changed how Python images are built.");
            }

            return steps;
        }));
    }

    private static PipelineStep Clone(PipelineStep step, Func<PipelineStepContext, Task> action) => new()
    {
        Name = step.Name,
        Description = $"{step.Description} (pycontainer-build)".Trim(),
        Action = action,
        Tags = [.. step.Tags],
        // pycontainer talks to registries directly, so the Docker/Podman readiness check is not needed.
        DependsOnSteps = [.. step.DependsOnSteps.Where(s => s != WellKnownPipelineSteps.CheckContainerRuntime)],
        RequiredBySteps = [.. step.RequiredBySteps],
        Resource = step.Resource,
    };
}

internal sealed class PyContainerBuildAnnotation(PyContainerBuildOptions options) : IResourceAnnotation
{
    public PyContainerBuildOptions Options { get; } = options;
}
